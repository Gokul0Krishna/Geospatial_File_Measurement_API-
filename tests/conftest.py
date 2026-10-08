from __future__ import annotations

import asyncio
import io
import os
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from fastapi import UploadFile
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.db.session import build_engine, build_sessionmaker
from app.main import create_app
from app.models import Base
from app.models.uploaded_file import UploadedFile
from app.services.lifecycle_service import LifecycleService
from app.services.processing_service import ProcessingService
from app.services.upload_service import UploadService
from app.storage.local import LocalStorage
from app.workers.dispatcher import JobDispatcher

# Point the whole suite at a real server instead of SQLite:
#   TEST_DATABASE_URL=postgresql+asyncpg://user:pass@localhost/test pytest
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture(autouse=True)
def _fresh_database() -> None:
    """SQLite gets a new file per test (tmp_path); a shared server needs an explicit reset."""
    if not TEST_DATABASE_URL:
        return

    async def reset() -> None:
        engine = create_async_engine(TEST_DATABASE_URL)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(reset())


class NoopDispatcher:
    """Pretends to queue work but never runs it: files stay PENDING (worker-mode shape)."""

    is_inline = False

    def __init__(self) -> None:
        self.enqueued: list[str] = []

    async def enqueue(self, file_id: str) -> None:
        self.enqueued.append(file_id)


class FailingDispatcher:
    is_inline = False

    async def enqueue(self, file_id: str) -> None:
        raise ConnectionError("redis is down")


@pytest.fixture
def make_settings(tmp_path: Path) -> Callable[..., Settings]:
    def _make(**overrides: Any) -> Settings:
        values: dict[str, Any] = {
            "DATABASE_URL": TEST_DATABASE_URL or f"sqlite+aiosqlite:///{tmp_path}/test.db",
            "STORAGE_DIR": tmp_path / "storage",
            "ENVIRONMENT": "test",
            "LOG_LEVEL": "WARNING",
            "LOG_JSON": False,
            "RUN_SWEEPER_IN_API": False,
            "READ_BATCH_SIZE": 5,  # tiny batches so tests exercise multi-batch paths
            "MAX_ACTIVE_FILES_PER_CLIENT": 100,
        }
        values.update(overrides)
        return Settings(_env_file=None, **values)

    return _make


@pytest.fixture
def make_client(make_settings: Callable[..., Settings]) -> Iterator[Callable[..., TestClient]]:
    clients: list[TestClient] = []

    def _make(dispatcher: JobDispatcher | None = None, **overrides: Any) -> TestClient:
        client = TestClient(create_app(make_settings(**overrides), dispatcher=dispatcher))
        client.__enter__()
        clients.append(client)
        return client

    yield _make
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def client(make_client: Callable[..., TestClient]) -> TestClient:
    return make_client()


def upload(
    client: TestClient, data: bytes, filename: str, *, headers: dict[str, str] | None = None
) -> Response:
    return client.post(
        "/api/files/",
        files={"file": (filename, data, "application/octet-stream")},
        headers=headers,
    )


@dataclass
class Env:
    settings: Settings
    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]
    storage: LocalStorage
    processing: ProcessingService
    lifecycle: LifecycleService
    upload: UploadService
    dispatcher: Any = field(default=None)

    async def ingest(self, data: bytes, filename: str, client_id: str = "tester") -> UploadedFile:
        return await self.upload.handle_upload(
            UploadFile(file=io.BytesIO(data), filename=filename), client_id
        )

    async def get(self, file_id: str) -> UploadedFile | None:
        async with self.sessionmaker() as session:
            return await session.get(UploadedFile, file_id)

    async def feature_count(self, file_id: str) -> int:
        from sqlalchemy import func, select

        from app.models.feature import Feature

        async with self.sessionmaker() as session:
            return int(
                (
                    await session.execute(
                        select(func.count()).select_from(Feature).where(Feature.file_id == file_id)
                    )
                ).scalar_one()
            )


@pytest_asyncio.fixture
async def make_env(
    make_settings: Callable[..., Settings],
) -> AsyncIterator[Callable[..., Any]]:
    created: list[Env] = []

    async def _make(dispatcher: JobDispatcher | None = None, **overrides: Any) -> Env:
        settings = make_settings(**overrides)
        engine = build_engine(settings)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sessionmaker = build_sessionmaker(engine)
        storage = LocalStorage(settings.STORAGE_DIR)
        dispatcher = dispatcher or NoopDispatcher()
        env = Env(
            settings=settings,
            engine=engine,
            sessionmaker=sessionmaker,
            storage=storage,
            processing=ProcessingService(sessionmaker, storage, settings),
            lifecycle=LifecycleService(sessionmaker, storage, settings, dispatcher),
            upload=UploadService(sessionmaker, storage, settings, dispatcher),
            dispatcher=dispatcher,
        )
        created.append(env)
        return env

    yield _make
    for env in created:
        await env.engine.dispose()
