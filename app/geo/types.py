"""Plain types shared by the pure geospatial layer (no I/O, no framework imports)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class FeatureStatus(StrEnum):
    MEASURED = "MEASURED"  # area/length computed
    REPAIRED = "REPAIRED"  # invalid polygon repaired with make_valid, then measured
    NOT_APPLICABLE = "NOT_APPLICABLE"  # points: nothing to measure
    UNSUPPORTED = "UNSUPPORTED"  # e.g. mixed GeometryCollection
    EMPTY = "EMPTY"  # null / empty geometry
    INVALID = "INVALID"  # invalid and not repairable
    ERROR = "ERROR"  # reprojection or decoding failed


class MeasurementKind(StrEnum):
    AREA = "area"
    LENGTH = "length"


@dataclass(slots=True)
class FeatureResult:
    """Outcome of measuring one feature. Values are in metres / square metres."""

    status: FeatureStatus
    geometry_type: str | None
    reason: str | None = None
    measurement_kind: MeasurementKind | None = None
    utm_crs: str | None = None
    utm_value: float | None = None
    geodesic_value: float | None = None
    warnings: list[str] = field(default_factory=list)
