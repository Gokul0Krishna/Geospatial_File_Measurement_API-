"""Geometry classification, normalisation and repair."""

from __future__ import annotations

from enum import StrEnum

import shapely
from shapely.geometry import MultiLineString, MultiPoint, MultiPolygon
from shapely.geometry.base import BaseGeometry
from shapely.validation import explain_validity


class Family(StrEnum):
    POLYGONAL = "polygonal"
    LINEAR = "linear"
    POINTAL = "pointal"
    OTHER = "other"


_POLYGONAL = {"Polygon", "MultiPolygon"}
_LINEAR = {"LineString", "MultiLineString", "LinearRing"}
_POINTAL = {"Point", "MultiPoint"}
_CONTAINERS = {"GeometryCollection", "MultiPolygon", "MultiLineString", "MultiPoint"}


def family_of(geom: BaseGeometry) -> Family:
    gtype = geom.geom_type
    if gtype in _POLYGONAL:
        return Family.POLYGONAL
    if gtype in _LINEAR:
        return Family.LINEAR
    if gtype in _POINTAL:
        return Family.POINTAL
    return Family.OTHER


def atoms(geom: BaseGeometry) -> list[BaseGeometry]:
    """Flatten collections and Multi* geometries into their single-part members."""
    out: list[BaseGeometry] = []
    stack = [geom]
    while stack:
        current = stack.pop()
        if current.geom_type in _CONTAINERS:
            stack.extend(shapely.get_parts(current))
        else:
            out.append(current)
    return [a for a in out if not a.is_empty]


def normalize(geom: BaseGeometry) -> BaseGeometry:
    """Drop Z and turn *homogeneous* GeometryCollections into Multi* geometries.

    KML ``<MultiGeometry>`` and some shapefiles produce GeometryCollections even when
    every member is a polygon (or line, or point).  Those are perfectly measurable, so
    we unwrap them.  Genuinely mixed collections are returned unchanged and end up as
    UNSUPPORTED.
    """
    geom = shapely.force_2d(geom)
    if geom.geom_type != "GeometryCollection":
        return geom
    parts = atoms(geom)
    kinds = {p.geom_type for p in parts}
    if kinds == {"Polygon"}:
        return parts[0] if len(parts) == 1 else MultiPolygon(parts)
    if kinds <= {"LineString", "LinearRing"} and kinds:
        return parts[0] if len(parts) == 1 else MultiLineString(parts)
    if kinds == {"Point"}:
        return parts[0] if len(parts) == 1 else MultiPoint(parts)
    return geom


def repair_polygonal(geom: BaseGeometry) -> BaseGeometry | None:
    """Repair an invalid polygon with ``make_valid`` and keep only the polygonal part.

    ``make_valid`` may return a GeometryCollection (polygons plus stray lines/points
    for degenerate input); only the polygons are measurable.
    """
    fixed = shapely.make_valid(geom)
    polygons = [p for p in atoms(fixed) if p.geom_type == "Polygon" and p.area > 0]
    if not polygons:
        return None
    return polygons[0] if len(polygons) == 1 else MultiPolygon(polygons)


def why_invalid(geom: BaseGeometry) -> str:
    return explain_validity(geom)
