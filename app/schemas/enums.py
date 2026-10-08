from __future__ import annotations

from enum import StrEnum

from app.geo.types import FeatureStatus, MeasurementKind

__all__ = ["FeatureStatus", "FileStatus", "MeasureMethod", "MeasurementKind", "SourceFormat"]


class FileStatus(StrEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    DELETING = "DELETING"


class MeasureMethod(StrEnum):
    UTM = "utm"  # planar, in the UTM zone (or UPS) of the feature
    GEODESIC = "geodesic"  # ellipsoidal, on the WGS84 ellipsoid


class SourceFormat(StrEnum):
    SHAPEFILE = "shapefile"
    KML = "kml"
