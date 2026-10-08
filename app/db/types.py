"""Column types: the *only* place that differs per database engine.

* Geometry is stored as WKB bytes in the source CRS: ``BYTEA`` on Postgres,
  ``LONGBLOB`` on MySQL (a plain ``BLOB`` caps at 64 KB), ``BLOB`` on SQLite.
* Properties use ``JSONB`` on Postgres and ``JSON`` elsewhere.
* Timestamps are stored in UTC and always come back timezone-aware.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, LargeBinary
from sqlalchemy.dialects import mysql, postgresql
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator, TypeEngine

GeometryBlob = LargeBinary().with_variant(mysql.LONGBLOB(), "mysql")
JSONType = JSON().with_variant(postgresql.JSONB(), "postgresql")


class UTCDateTime(TypeDecorator[datetime]):
    impl = DateTime(timezone=True)
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "mysql":
            return dialect.type_descriptor(mysql.DATETIME(fsp=6))
        return dialect.type_descriptor(DateTime(timezone=True))

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime passed to a UTCDateTime column")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value
