from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.enums import FeatureStatus, MeasurementKind, MeasureMethod
from app.schemas.file import FileSummary


class MeasurementOut(BaseModel):
    type: MeasurementKind
    value: float
    unit: Literal["m2", "m"]
    method: MeasureMethod
    crs: str = Field(
        description="CRS the value was computed in (utm), or the ellipsoid used (geodesic)."
    )


class MeasurementItem(BaseModel):
    feature_index: int
    geometry_type: str | None
    status: FeatureStatus
    reason: str | None = None
    measurement: MeasurementOut | None = None
    warnings: list[str] = Field(default_factory=list)
    geometry: dict[str, Any] | None = Field(
        None, description="GeoJSON geometry in the file's CRS; only with include_geometry=true."
    )


class FeatureOut(MeasurementItem):
    crs: str | None = Field(None, description="CRS of `geometry` (the source CRS of the file).")
    properties: dict[str, Any] = Field(default_factory=dict)


class MeasurementPage(BaseModel):
    file_id: str
    method: MeasureMethod
    crs: str | None
    summary: FileSummary | None = None
    count: int
    next_cursor: str | None = None
    items: list[MeasurementItem]


class FeaturePage(BaseModel):
    file_id: str
    method: MeasureMethod
    crs: str | None
    count: int
    next_cursor: str | None = None
    items: list[FeatureOut]
