from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.models.uploaded_file import UploadedFile
from app.schemas.enums import FileStatus, SourceFormat


class FileSummary(BaseModel):
    """Aggregates computed once during processing (so reads stay O(1))."""

    by_status: dict[str, int] = Field(default_factory=dict)
    by_geometry_type: dict[str, int] = Field(default_factory=dict)
    total_area_m2: dict[str, float] = Field(
        default_factory=dict, description="Sum of polygon areas per method (utm, geodesic)."
    )
    total_length_m: dict[str, float] = Field(
        default_factory=dict, description="Sum of line lengths per method (utm, geodesic)."
    )


class FileOut(BaseModel):
    id: str
    filename: str
    source_format: SourceFormat
    size_bytes: int
    status: FileStatus
    feature_count: int | None = Field(None, description="Known once processing completes.")
    processed_features: int = 0
    crs: str | None = Field(None, description="Source CRS of the file, e.g. EPSG:4326.")
    error: str | None = None
    warnings: list[str] = Field(default_factory=list)
    summary: FileSummary | None = None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None

    @classmethod
    def from_record(cls, record: UploadedFile) -> FileOut:
        stats: dict[str, Any] | None = record.stats
        return cls(
            id=record.id,
            filename=record.filename,
            source_format=SourceFormat(record.source_format),
            size_bytes=record.size_bytes,
            status=FileStatus(record.status),
            feature_count=record.feature_count,
            processed_features=record.processed_features or 0,
            crs=record.crs,
            error=record.error,
            warnings=list(record.warnings or []),
            summary=FileSummary.model_validate(stats) if stats else None,
            created_at=record.created_at,
            updated_at=record.updated_at,
            completed_at=record.completed_at,
        )


class FilePage(BaseModel):
    items: list[FileOut]
    next_cursor: str | None = None


class DeleteOut(BaseModel):
    id: str
    status: FileStatus
