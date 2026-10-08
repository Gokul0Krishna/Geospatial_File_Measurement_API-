from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.clock import utcnow
from app.db.types import JSONType, UTCDateTime
from app.models.base import Base


class UploadedFile(Base):
    __tablename__ = "files"
    __table_args__ = (Index("ix_files_client_status", "client_id", "status"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    source_format: Mapped[str] = mapped_column(String(16))  # "shapefile" | "kml"
    storage_key: Mapped[str] = mapped_column(String(300))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), index=True)

    feature_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    processed_features: Mapped[int] = mapped_column(Integer, default=0)
    crs: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    warnings: Mapped[list[str]] = mapped_column(JSONType, default=list)
    stats: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    client_id: Mapped[str] = mapped_column(String(64), default="unknown")

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
