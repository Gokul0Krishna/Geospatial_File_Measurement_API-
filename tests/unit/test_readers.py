import math
from datetime import date, datetime
from decimal import Decimal

import shapely

from app.geo.crs import crs_label
from app.geo.readers.base import json_safe
from app.geo.readers.registry import get_reader
from app.utils.zip_safety import ZipLimits, extract_shapefile, inspect_shapefile_zip
from tests.helpers import (
    BLR_HOLE,
    BLR_SQUARE,
    build_shapefile_zip,
    kml_document,
    kml_line,
    kml_placemark,
    kml_point,
    kml_polygon,
    write,
)

LIMITS = ZipLimits(100, 10**9, 100, 1 << 20)


def shapefile_reader(tmp_path, data):
    zpath = write(tmp_path / "s.zip", data)
    plan = inspect_shapefile_zip(zpath, LIMITS, require_prj=True)
    return get_reader("shapefile", extract_shapefile(zpath, plan, tmp_path / "x", LIMITS))


def test_shapefile_reader_streams_in_batches_with_attributes_and_null_shapes(tmp_path):
    data = build_shapefile_zip(
        "polygon",
        [[BLR_SQUARE, BLR_HOLE], [BLR_SQUARE], None],
        records=[("A", 1.5), ("B", 2.0), ("C", None)],
    )
    reader = shapefile_reader(tmp_path, data)
    info = reader.inspect()
    assert info.feature_count == 3
    assert crs_label(info.crs) == "EPSG:4326"

    batches = list(reader.iter_batches(2))
    assert [len(b.wkb) for b in batches] == [2, 1]
    assert batches[0].properties[0] == {"name": "A", "value": 1.5}
    assert batches[1].wkb == [None]  # null shape
    assert batches[1].properties[0] == {"name": "C", "value": None}
    assert shapely.from_wkb(batches[0].wkb[0]).geom_type == "Polygon"


def test_kml_reader_walks_every_folder_and_cleans_properties(tmp_path):
    kml = kml_document(
        (None, [kml_placemark("loose", kml_point(77.5, 12.9))]),
        ("Parcels", [kml_placemark("P1", kml_polygon(BLR_SQUARE, [BLR_HOLE]), {"owner": "Alice"}), kml_placemark("nogeom")]),
        ("Roads", [kml_placemark("R1", kml_line([(77.59, 12.97), (77.6, 12.99)]))]),
    )
    reader = get_reader("kml", write(tmp_path / "t.kml", kml))
    info = reader.inspect()
    assert info.feature_count == 4
    assert len(info.layers) == 3  # every <Folder> is a GDAL layer
    assert crs_label(info.crs) == "EPSG:4326"

    props, wkb = [], []
    for batch in reader.iter_batches(100):
        props += batch.properties
        wkb += batch.wkb
    assert [p["name"] for p in props] == ["loose", "P1", "nogeom", "R1"]
    assert props[1]["owner"] == "Alice"
    assert all("tessellate" not in p and "visibility" not in p for p in props)  # GDAL noise dropped
    assert wkb[2] is None  # placemark without geometry


def test_kml_multigeometry_comes_through_as_multipolygon(tmp_path):
    mg = f"<MultiGeometry>{kml_polygon(BLR_SQUARE)}{kml_polygon([(77.61, 12.97), (77.62, 12.97), (77.62, 12.98)])}</MultiGeometry>"
    reader = get_reader("kml", write(tmp_path / "m.kml", kml_document(("F", [kml_placemark("mg", mg)]))))
    batch = next(iter(reader.iter_batches(10)))
    assert shapely.from_wkb(batch.wkb[0]).geom_type == "MultiPolygon"


def test_json_safe_handles_non_json_values():
    assert json_safe(float("nan")) is None
    assert json_safe(math.inf) is None
    assert json_safe("a\x00b") == "ab"  # Postgres rejects NUL in JSON text
    assert json_safe(date(2026, 1, 2)) == "2026-01-02"
    assert json_safe(datetime(2026, 1, 2, 3, 4, 5)) == "2026-01-02T03:04:05"
    assert json_safe(Decimal("1.50")) == 1.5
    assert json_safe(b"\x01\x02") == "AQI="
    assert json_safe({"a": [1, float("nan")]}) == {"a": [1, None]}
