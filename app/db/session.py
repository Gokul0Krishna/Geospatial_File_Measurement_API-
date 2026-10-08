from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings


def build_engine(settings: Settings) -> AsyncEngine:
    url = make_url(settings.DATABASE_URL)
    backend = url.get_backend_name()
    kwargs: dict[str, Any] = {"pool_pre_ping": True}

    if backend == "sqlite":
        if url.database and url.database != ":memory:":
            Path(url.database).parent.mkdir(parents=True, exist_ok=True)
        kwargs["connect_args"] = {"timeout": 30}
    else:
        kwargs.update(
            pool_size=settings.DB_POOL_SIZE,
            max_overflow=settings.DB_MAX_OVERFLOW,
            # Make status polling behave the same on every engine (MySQL defaults to
            # REPEATABLE READ, Postgres to READ COMMITTED).
            isolation_level="READ COMMITTED",
        )
        if backend == "postgresql":
            kwargs["connect_args"] = {
                "server_settings": {"statement_timeout": str(settings.DB_STATEMENT_TIMEOUT_MS)}
            }
        elif backend == "mysql":
            kwargs["connect_args"] = {
                "init_command": f"SET SESSION max_execution_time={settings.DB_STATEMENT_TIMEOUT_MS}"
            }

    engine = create_async_engine(url, **kwargs)

    if backend == "sqlite":

        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_connection: sqlite3.Connection, _record: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.close()

    return engine


def build_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
