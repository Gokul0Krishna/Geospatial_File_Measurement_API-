"""Fixture builders: real shapefiles (written with pyshp, independent of the read stack)
and KML documents, so tests never depend on committed binary files."""

from __future__ import annotations

import io
import warnings
import zipfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import shapefile  # pyshp
from pyproj import CRS, Geod

Ring = Sequence[tuple[float, float]]
GEOD = Geod(ellps="WGS84")

# ~1 km x 1 km square near Bengaluru (UTM zone 43N)
BLR_SQUARE: list[tuple[float, float]] = [
    (77.590, 12.970),
    (77.599, 12.970),
    (77.599, 12.979),
    (77.590, 12.979),
]
BLR_HOLE: list[tuple[float, float]] = [
    (77.593, 12.973),
    (77.596, 12.973),
    (77.596, 12.976),
    (77.593, 12.976),
]


def geodesic_ring_area(ring: Ring) -> float:
    lons = [p[0] for p in ring]
    lats = [p[1] for p in ring]
    area, _ = GEOD.polygon_area_perimeter(lons, lats)
    return abs(area)


def geodesic_line_length(coords: Ring) -> float:
    return GEOD.line_length([p[0] for p in coords], [p[1] for p in coords])


def _signed_area(ring: Ring) -> float:
    return sum(
        x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(ring, [*ring[1:], ring[0]], strict=True)
    ) / 2.0


def _closed(ring: Ring) -> list[tuple[float, float]]:
    pts = list(ring)
    return pts if pts[0] == pts[-1] else [*pts, pts[0]]


def _orient(ring: Ring, *, clockwise: bool) -> list[tuple[float, float]]:
    pts = _closed(ring)
    is_ccw = _signed_area(pts[:-1]) > 0
    return pts if is_ccw != clockwise else pts[::-1]


def build_shapefile_zip(
    kind: str,
    shapes: Sequence[Any],
    *,
    crs: CRS | None = None,
    include_prj: bool = True,
    records: Sequence[Sequence[Any]] | None = None,
    fields: Sequence[tuple[str, str, int, int]] = (("name", "C", 40, 0), ("value", "N", 12, 3)),
) -> bytes:
    """``kind``: polygon | line | point. ``shapes`` items: polygon -> [exterior, *holes];
    line -> coordinate list; point -> (x, y); ``None`` -> null shape."""
    crs = crs or CRS.from_epsg(4326)
    shp, shx, dbf = io.BytesIO(), io.BytesIO(), io.BytesIO()
    shape_type = {"polygon": shapefile.POLYGON, "line": shapefile.POLYLINE, "point": shapefile.POINT}[kind]
    with shapefile.Writer(shp=shp, shx=shx, dbf=dbf, shapeType=shape_type) as writer:
        for name, ftype, size, dec in fields:
            writer.field(name, ftype, size, dec)
        for i, shape in enumerate(shapes):
            if shape is None:
                writer.null()
            elif kind == "polygon":
                rings = [_orient(shape[0], clockwise=True)] + [
                    _orient(h, clockwise=False) for h in shape[1:]
                ]
                writer.poly(rings)
            elif kind == "line":
                writer.line([list(shape)])
            else:
                writer.point(*shape)
            row = records[i] if records else (f"feature-{i}", float(i))
            writer.record(*row)

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("layer.shp", shp.getvalue())
        zf.writestr("layer.shx", shx.getvalue())
        zf.writestr("layer.dbf", dbf.getvalue())
        zf.writestr("layer.cpg", b"UTF-8")
        if include_prj:
            zf.writestr("layer.prj", crs.to_wkt(version="WKT1_ESRI"))
    return out.getvalue()


def build_zip(entries: dict[str, bytes], *, allow_duplicates: Sequence[tuple[str, bytes]] = ()) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            for name, data in allow_duplicates:
                zf.writestr(name, data)
    return out.getvalue()


def _coords(points: Ring, z: float | None = 0.0) -> str:
    return " ".join(f"{x},{y}" + (f",{z}" if z is not None else "") for x, y in points)


def kml_polygon(exterior: Ring, holes: Sequence[Ring] = ()) -> str:
    inner = "".join(
        f"<innerBoundaryIs><LinearRing><coordinates>{_coords(_closed(h))}</coordinates>"
        "</LinearRing></innerBoundaryIs>"
        for h in holes
    )
    return (
        "<Polygon><outerBoundaryIs><LinearRing>"
        f"<coordinates>{_coords(_closed(exterior))}</coordinates>"
        f"</LinearRing></outerBoundaryIs>{inner}</Polygon>"
    )


def kml_line(coords: Ring) -> str:
    return f"<LineString><coordinates>{_coords(coords)}</coordinates></LineString>"


def kml_point(x: float, y: float) -> str:
    return f"<Point><coordinates>{x},{y},0</coordinates></Point>"


def kml_placemark(name: str, geometry: str = "", extended: dict[str, str] | None = None) -> str:
    data = ""
    if extended:
        data = "<ExtendedData>" + "".join(
            f'<Data name="{k}"><value>{v}</value></Data>' for k, v in extended.items()
        ) + "</ExtendedData>"
    return f"<Placemark><name>{name}</name>{data}{geometry}</Placemark>"


def kml_document(*folders: tuple[str | None, Sequence[str]]) -> bytes:
    """``folders``: (folder name or None for top level, placemark xml strings)."""
    body = ""
    for folder_name, placemarks in folders:
        if folder_name is None:
            body += "".join(placemarks)
        else:
            body += f"<Folder><name>{folder_name}</name>{''.join(placemarks)}</Folder>"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
        f"{body}</Document></kml>"
    ).encode()


def write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path
