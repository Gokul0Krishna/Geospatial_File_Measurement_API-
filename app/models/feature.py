from __future__ import annotations

from typing import Any

from sqlalchemy import Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.types import GeometryBlob, JSONType
from app.models.base import Base


class Feature(Base):
    """One row per feature.  The composite primary key ``(file_id, feature_index)`` is
    also the only index we need: every query is "features of file X, after index N"."""

    __tablename__ = "features"

    file_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("files.id", ondelete="CASCADE"), primary_key=True
    )
    feature_index: Mapped[int] = mapped_column(Integer, primary_key=True)

    geometry_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    geometry_wkb: Mapped[bytes | None] = mapped_column(GeometryBlob, nullable=True)
    properties: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)

    status: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    measurement_kind: Mapped[str | None] = mapped_column(String(8), nullable=True)
    utm_crs: Mapped[str | None] = mapped_column(String(16), nullable=True)
    utm_value: Mapped[float | None] = mapped_column(Float(53), nullable=True)
    geodesic_value: Mapped[float | None] = mapped_column(Float(53), nullable=True)
    warnings: Mapped[list[str]] = mapped_column(JSONType, default=list)
