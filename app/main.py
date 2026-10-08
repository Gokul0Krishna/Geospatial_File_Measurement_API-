from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import FastAPI

from app import __version__
from app.api.middleware import RequestContextMiddleware
from app.api.routes import files, health
from app.core.config import Settings, get_settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging
from app.db.session import build_engine, build_sessionmaker
from app.models import Base
from app.services.lifecycle_service import LifecycleService
from app.services.processing_service import ProcessingService
from app.services.upload_service import UploadService
from app.storage.local import LocalStorage
from app.workers.dispatcher import ArqDispatcher, InlineDispatcher, JobDispatcher

logger = logging.getLogger(__name__)

# multipart framing adds a little on top of the file itself
_MULTIPART_OVERHEAD = 1024 * 1024

DESCRIPTION = """
Upload a **Shapefile (.zip)** or **KML** and get per-feature **area** (polygons) and
**length** (lines).

Measurements are never computed in degrees: every feature is reprojected to its UTM
zone (UPS near the poles) first, and an independent geodesic value on the WGS84
ellipsoid is stored alongside (`?method=utm|geodesic`).
"""


async def _sweeper_loop(lifecycle: LifecycleService, interval_s: int) -> None:
    while True:
        await asyncio.sleep(interval_s)
        try:
            await lifecycle.run_sweep()
        except Exception:
            logger.exception("sweep failed")


def create_app(settings: Settings | None = None, *, dispatcher: JobDispatcher | None = None) -> FastAPI:
    """App factory. ``settings`` / ``dispatcher`` overrides exist for tests."""
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings.LOG_LEVEL, settings.LOG_JSON)
        engine = build_engine(settings)
        sessionmaker = build_sessionmaker(engine)
        if settings.AUTO_CREATE_TABLES:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

        storage = LocalStorage(settings.STORAGE_DIR)
        processing = ProcessingService(sessionmaker, storage, settings)

        redis_pool = None
        active_dispatcher = dispatcher
        if active_dispatcher is None:
            if settings.PROCESSING_MODE == "inline":
                active_dispatcher = InlineDispatcher(processing)
            else:
                redis_pool = await create_pool(RedisSettings.from_dsn(settings.REDIS_URL))
                active_dispatcher = ArqDispatcher(redis_pool)

        lifecycle = LifecycleService(sessionmaker, storage, settings, active_dispatcher)
        app.state.settings = settings
        app.state.engine = engine
        app.state.sessionmaker = sessionmaker
        app.state.storage = storage
        app.state.redis = redis_pool
        app.state.upload_service = UploadService(sessionmaker, storage, settings, active_dispatcher)
        app.state.lifecycle_service = lifecycle

        sweeper: asyncio.Task[None] | None = None
        if settings.PROCESSING_MODE == "inline" and settings.RUN_SWEEPER_IN_API:
            sweeper = asyncio.create_task(_sweeper_loop(lifecycle, settings.SWEEP_INTERVAL_S))

        logger.info("application started", extra={"mode": settings.PROCESSING_MODE})
        try:
            yield
        finally:
            if sweeper is not None:
                sweeper.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await sweeper
            if redis_pool is not None:
                await redis_pool.aclose()
            await engine.dispose()

    app = FastAPI(
        title=settings.APP_NAME,
        version=__version__,
        description=DESCRIPTION,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.add_middleware(
        RequestContextMiddleware,
        max_body_bytes=settings.MAX_UPLOAD_BYTES + _MULTIPART_OVERHEAD,
    )
    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(files.router)
    return app

