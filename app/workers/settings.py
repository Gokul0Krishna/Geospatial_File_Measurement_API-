"""``arq app.workers.settings.WorkerSettings`` starts a worker."""

from __future__ import annotations

from typing import Any

from arq import cron
from arq.connections import RedisSettings

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import build_engine, build_sessionmaker
from app.services.lifecycle_service import LifecycleService
from app.services.processing_service import ProcessingService
from app.storage.local import LocalStorage
from app.workers.tasks import process_file, sweep

_settings = get_settings()


async def startup(ctx: dict[str, Any]) -> None:
    configure_logging(_settings.LOG_LEVEL, _settings.LOG_JSON)
    engine = build_engine(_settings)
    sessionmaker = build_sessionmaker(engine)
    storage = LocalStorage(_settings.STORAGE_DIR)
    ctx["engine"] = engine
    ctx["settings"] = _settings
    ctx["processing"] = ProcessingService(sessionmaker, storage, _settings)
    ctx["lifecycle"] = LifecycleService(sessionmaker, storage, _settings)


async def shutdown(ctx: dict[str, Any]) -> None:
    await ctx["engine"].dispose()


def _sweep_minutes(interval_s: int) -> set[int]:
    return set(range(0, 60, max(1, interval_s // 60)))


class WorkerSettings:
    functions = [process_file]
    cron_jobs = [cron(sweep, minute=_sweep_minutes(_settings.SWEEP_INTERVAL_S), unique=True)]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(_settings.REDIS_URL)
    max_jobs = _settings.WORKER_MAX_JOBS
    job_timeout = _settings.JOB_TIMEOUT_S + 30  # our own deadline fires first
    max_tries = _settings.JOB_MAX_TRIES
    retry_jobs = True
    keep_result = 3600
