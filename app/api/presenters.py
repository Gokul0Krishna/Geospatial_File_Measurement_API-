"""Database rows -> API response models."""

from __future__ import annotations

from typing import Any

import shapely
from shapely.geometry import mapping

from app.models.feature import Feature
from app.schemas.enums import FeatureStatus, MeasurementKind, MeasureMethod
from app.schemas.feature import FeatureOut, MeasurementItem, MeasurementOut

GEODESIC_LABEL = "ellipsoid:WGS84"


def geometry_to_geojson(wkb: bytes | None) -> dict[str, Any] | None:
    if wkb is None:
        return None
    return dict(mapping(shapely.from_wkb(wkb)))


def build_measurement(row: Feature, method: MeasureMethod) -> MeasurementOut | None:
    if row.measurement_kind is None:
        return None
    value = row.utm_value if method is MeasureMethod.UTM else row.geodesic_value
    if value is None:
        return None
    kind = MeasurementKind(row.measurement_kind)
    return MeasurementOut(
        type=kind,
        value=value,
        unit="m2" if kind is MeasurementKind.AREA else "m",
        method=method,
        crs=(row.utm_crs or "unknown") if method is MeasureMethod.UTM else GEODESIC_LABEL,
    )


def _common(row: Feature, method: MeasureMethod) -> dict[str, Any]:
    return {
        "feature_index": row.feature_index,
        "geometry_type": row.geometry_type,
        "status": FeatureStatus(row.status),
        "reason": row.reason,
        "measurement": build_measurement(row, method),
        "warnings": list(row.warnings or []),
    }


def to_measurement_item(row: Feature, method: MeasureMethod, *, include_geometry: bool) -> MeasurementItem:
    data = _common(row, method)
    if include_geometry:  # left unset otherwise so it is omitted from the JSON
        data["geometry"] = geometry_to_geojson(row.geometry_wkb)
    return MeasurementItem(**data)


def to_feature(
    row: Feature, file_crs: str | None, method: MeasureMethod, *, include_geometry: bool
) -> FeatureOut:
    data = _common(row, method)
    data["crs"] = file_crs
    data["properties"] = dict(row.properties or {})
    if include_geometry:
        data["geometry"] = geometry_to_geojson(row.geometry_wkb)
    return FeatureOut(**data)


def select_page(
    rows: list[Feature], limit: int, *, with_geometry: bool, max_geometry_bytes: int
) -> tuple[list[Feature], bool]:
    """Apply the page limit plus a byte budget on geometries (summed WKB size), so a few
    huge polygons cannot produce a multi-megabyte response."""
    page: list[Feature] = []
    used = 0
    for row in rows[:limit]:
        if with_geometry:
            size = len(row.geometry_wkb or b"")
            if page and used + size > max_geometry_bytes:
                break
            used += size
        page.append(row)
    return page, len(rows) > len(page)
