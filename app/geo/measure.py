"""Area / length measurement.

Pipeline for a batch of geometries in the file's CRS:

1. classify each geometry (polygonal / linear / point / other), validate and repair
2. reproject everything to WGS84 (lon/lat)
3. for every feature pick the UTM zone (or UPS near the poles) of its extent centre
4. reproject per UTM group, vectorised, and compute planar ``area`` / ``length``
5. compute the geodesic value on the WGS84 ellipsoid as an independent cross-check

We *always* go through WGS84 -> UTM, even when the source CRS is already projected:
a projected source can be non-metric (feet) or wildly non-equal-area (Web Mercator
inflates areas by 1/cos^2(lat)), so trusting it would be wrong far more often than
re-projecting is.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import shapely
from pyproj import CRS, Geod
from shapely.geometry.base import BaseGeometry

from app.geo.crs import (
    UPS_NORTH,
    UPS_SOUTH,
    WGS84,
    get_transformer,
    is_wgs84,
    transform_geometries,
    utm_epsgs_for,
    utm_zone,
)
from app.geo.geometry import Family, family_of, normalize, repair_polygonal, why_invalid
from app.geo.types import FeatureResult, FeatureStatus, MeasurementKind

# Warning codes attached to individual features.
W_LARGE_EXTENT = "LARGE_EXTENT"
W_MULTI_ZONE = "SPANS_MULTIPLE_UTM_ZONES"
W_ANTIMERIDIAN = "POSSIBLE_ANTIMERIDIAN_CROSSING"
W_POLAR = "POLAR_REGION"
W_NO_GEODESIC = "GEODESIC_UNAVAILABLE"

_SOURCE_RANGE_ERROR = (
    "Coordinates fall outside the valid longitude/latitude range for a geographic CRS; "
    "the declared CRS is probably wrong."
)
_REPROJECTION_ERROR = (
    "Reprojection failed (non-finite coordinates); the geometry is probably outside the "
    "area of use of the declared CRS."
)


@dataclass(slots=True)
class _Candidate:
    index: int  # position in the input batch
    geom: BaseGeometry
    family: Family
    result: FeatureResult


def _object_array(items: Sequence[BaseGeometry | None]) -> np.ndarray:
    arr = np.empty(len(items), dtype=object)
    for i, item in enumerate(items):
        arr[i] = item
    return arr


# ---------------------------------------------------------------------------- geodesic


def geodesic_area(geod: Geod, geom: BaseGeometry) -> float:
    """Ellipsoidal area in m^2 of a (Multi)Polygon in lon/lat degrees, holes subtracted."""
    total = 0.0
    for poly in shapely.get_parts(geom):
        ring = shapely.get_coordinates(poly.exterior)
        area, _ = geod.polygon_area_perimeter(ring[:, 0], ring[:, 1])
        piece = abs(area)
        for hole in poly.interiors:
            hole_xy = shapely.get_coordinates(hole)
            hole_area, _ = geod.polygon_area_perimeter(hole_xy[:, 0], hole_xy[:, 1])
            piece -= abs(hole_area)
        total += piece
    return total


def geodesic_length(geod: Geod, geom: BaseGeometry) -> float:
    """Ellipsoidal length in metres of a (Multi)LineString in lon/lat degrees."""
    total = 0.0
    for line in shapely.get_parts(geom):
        xy = shapely.get_coordinates(line)
        total += geod.line_length(xy[:, 0], xy[:, 1])
    return total


# ---------------------------------------------------------------------------- batch


def _prepare(index: int, geom: BaseGeometry | None) -> _Candidate | FeatureResult:
    """Classify one geometry. Returns a final result, or a candidate to be measured."""
    if geom is None:
        return FeatureResult(FeatureStatus.EMPTY, None, reason="Feature has no geometry.")
    original_type = geom.geom_type
    if geom.is_empty:
        return FeatureResult(FeatureStatus.EMPTY, original_type, reason="Geometry is empty.")

    geom = normalize(geom)
    family = family_of(geom)
    if family is Family.POINTAL:
        return FeatureResult(
            FeatureStatus.NOT_APPLICABLE,
            original_type,
            reason="Points have no area or length.",
        )
    if family is Family.OTHER:
        return FeatureResult(
            FeatureStatus.UNSUPPORTED,
            original_type,
            reason=f"{original_type} geometries are not supported for measurement.",
        )

    kind = MeasurementKind.AREA if family is Family.POLYGONAL else MeasurementKind.LENGTH
    result = FeatureResult(FeatureStatus.MEASURED, original_type, measurement_kind=kind)

    if not geom.is_valid:
        explanation = why_invalid(geom)
        repaired = repair_polygonal(geom) if family is Family.POLYGONAL else None
        if repaired is None:
            result.status = FeatureStatus.INVALID
            result.measurement_kind = None
            result.reason = f"Invalid geometry could not be repaired: {explanation}"
            return result
        geom = repaired
        result.status = FeatureStatus.REPAIRED
        result.reason = f"Invalid polygon repaired with make_valid before measuring: {explanation}"
    return _Candidate(index, geom, family, result)


def measure_batch(
    geoms: Sequence[BaseGeometry | None],
    source_crs: CRS,
    *,
    long_extent_deg: float = 3.0,
) -> list[FeatureResult]:
    """Measure a batch of geometries given in ``source_crs``. Never raises per feature."""
    results: list[FeatureResult | None] = [None] * len(geoms)
    candidates: list[_Candidate] = []

    for i, geom in enumerate(geoms):
        try:
            prepared = _prepare(i, geom)
        except Exception as exc:  # defensive: one bad feature must not sink the batch
            results[i] = FeatureResult(
                FeatureStatus.ERROR,
                getattr(geom, "geom_type", None),
                reason=f"Unexpected error while classifying geometry: {exc.__class__.__name__}",
            )
            continue
        if isinstance(prepared, FeatureResult):
            results[i] = prepared
        else:
            candidates.append(prepared)

    if candidates:
        _measure_candidates(candidates, source_crs, long_extent_deg)
        for cand in candidates:
            results[cand.index] = cand.result

    assert all(r is not None for r in results)
    return [r for r in results if r is not None]


def _fail(cand: _Candidate, reason: str) -> None:
    cand.result.status = FeatureStatus.ERROR
    cand.result.reason = reason
    cand.result.measurement_kind = None
    cand.result.utm_crs = None
    cand.result.utm_value = None
    cand.result.geodesic_value = None


def _measure_candidates(candidates: list[_Candidate], source_crs: CRS, long_extent_deg: float) -> None:
    count = len(candidates)
    arr = _object_array([c.geom for c in candidates])
    ok = np.ones(count, dtype=bool)

    # 1. sanity-check coordinates against the declared geographic CRS
    if source_crs.is_geographic:
        b = shapely.bounds(arr)
        bad = (b[:, 0] < -180) | (b[:, 2] > 180) | (b[:, 1] < -90) | (b[:, 3] > 90)
        for j in np.flatnonzero(bad):
            _fail(candidates[j], _SOURCE_RANGE_ERROR)
        ok &= ~bad

    # 2. to WGS84
    wgs = arr if is_wgs84(source_crs) else transform_geometries(arr, get_transformer(source_crs, WGS84))
    b = shapely.bounds(wgs)
    finite = np.isfinite(b).all(axis=1)
    in_range = (np.abs(b[:, 0]) <= 180.0) & (np.abs(b[:, 1]) <= 90.0)
    bad = ok & ~(finite & in_range)
    for j in np.flatnonzero(bad):
        _fail(candidates[j], _REPROJECTION_ERROR)
    ok &= ~bad
    if not ok.any():
        return

    # 3. UTM zone per feature (centre of its extent)
    idx = np.flatnonzero(ok)
    centre_lon = (b[idx, 0] + b[idx, 2]) / 2.0
    centre_lat = (b[idx, 1] + b[idx, 3]) / 2.0
    codes = utm_epsgs_for(centre_lon, centre_lat)

    lon_extent = b[idx, 2] - b[idx, 0]
    zone_min, zone_max = utm_zone(b[idx, 0]), utm_zone(b[idx, 2])
    for k, j in enumerate(idx):
        warnings = candidates[j].result.warnings
        if lon_extent[k] > long_extent_deg:
            warnings.append(W_LARGE_EXTENT)
        if zone_min[k] != zone_max[k]:
            warnings.append(W_MULTI_ZONE)
        if lon_extent[k] > 180.0:
            warnings.append(W_ANTIMERIDIAN)
        if codes[k] in (UPS_NORTH, UPS_SOUTH):
            warnings.append(W_POLAR)

    # 4. planar measurement in the projected CRS, vectorised per UTM group
    for code in np.unique(codes):
        members = idx[codes == code]
        projected = transform_geometries(wgs[members], get_transformer(WGS84, int(code)))
        areas = shapely.area(projected)
        lengths = shapely.length(projected)
        for k, j in enumerate(members):
            cand = candidates[j]
            value = areas[k] if cand.family is Family.POLYGONAL else lengths[k]
            if not np.isfinite(value):
                _fail(cand, _REPROJECTION_ERROR)
                ok[j] = False
                continue
            cand.result.utm_crs = f"EPSG:{int(code)}"
            cand.result.utm_value = float(value)

    # 5. geodesic cross-check (ellipsoidal, no projection involved)
    geod = Geod(ellps="WGS84")
    for j in np.flatnonzero(ok):
        cand = candidates[j]
        try:
            if cand.family is Family.POLYGONAL:
                cand.result.geodesic_value = geodesic_area(geod, wgs[j])
            else:
                cand.result.geodesic_value = geodesic_length(geod, wgs[j])
        except Exception:
            cand.result.warnings.append(W_NO_GEODESIC)
