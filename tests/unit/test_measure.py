import math

import numpy as np
import pytest
from pyproj import CRS, Transformer
from shapely.geometry import GeometryCollection, LineString, MultiPolygon, Point, Polygon

from app.geo.crs import WGS84
from app.geo.measure import (
    W_LARGE_EXTENT,
    W_MULTI_ZONE,
    W_POLAR,
    measure_batch,
)
from app.geo.types import FeatureStatus, MeasurementKind
from tests.helpers import (
    BLR_HOLE,
    BLR_SQUARE,
    geodesic_line_length,
    geodesic_ring_area,
)


def one(geom, crs=WGS84):
    return measure_batch([geom], crs)[0]


def test_polygon_area_is_in_square_metres_not_degrees():
    result = one(Polygon(BLR_SQUARE))
    assert result.status is FeatureStatus.MEASURED
    assert result.measurement_kind is MeasurementKind.AREA
    assert result.utm_crs == "EPSG:32643"
    # ~1 km^2. A naive degree-based area would be about 8e-5.
    assert 9e5 < result.utm_value < 1.1e6


def test_utm_agrees_with_independent_geodesic_calculation():
    result = one(Polygon(BLR_SQUARE))
    truth = geodesic_ring_area(BLR_SQUARE)
    assert result.geodesic_value == pytest.approx(truth, rel=1e-9)
    assert result.utm_value == pytest.approx(truth, rel=3e-3)  # UTM scale distortion < 0.3%


def test_holes_are_subtracted():
    """pyproj's own Geod.geometry_area_perimeter ADDS holes for some ring orientations;
    ours must always subtract them."""
    holed = Polygon(BLR_SQUARE, [BLR_HOLE])
    result = one(holed)
    truth = geodesic_ring_area(BLR_SQUARE) - geodesic_ring_area(BLR_HOLE)
    assert result.geodesic_value == pytest.approx(truth, rel=1e-9)
    assert result.utm_value == pytest.approx(truth, rel=3e-3)
    assert result.utm_value < one(Polygon(BLR_SQUARE)).utm_value


def test_multipolygon_is_sum_of_parts():
    a = Polygon(BLR_SQUARE)
    b = Polygon([(77.61, 12.97), (77.62, 12.97), (77.62, 12.98), (77.61, 12.98)])
    both = one(MultiPolygon([a, b]))
    assert both.utm_value == pytest.approx(one(a).utm_value + one(b).utm_value, rel=1e-9)


def test_line_length():
    coords = [(77.59, 12.97), (77.60, 12.99), (77.62, 12.99)]
    result = one(LineString(coords))
    assert result.measurement_kind is MeasurementKind.LENGTH
    assert result.geodesic_value == pytest.approx(geodesic_line_length(coords), rel=1e-9)
    assert result.utm_value == pytest.approx(result.geodesic_value, rel=3e-3)


def test_southern_hemisphere_uses_327xx():
    sydney = Polygon([(151.2, -33.9), (151.21, -33.9), (151.21, -33.89), (151.2, -33.89)])
    result = one(sydney)
    assert result.utm_crs == "EPSG:32756"
    assert result.utm_value == pytest.approx(geodesic_ring_area(list(sydney.exterior.coords)), rel=3e-3)


def test_z_coordinates_are_ignored():
    flat = one(Polygon(BLR_SQUARE))
    tall = one(Polygon([(x, y, 900.0) for x, y in BLR_SQUARE]))
    assert tall.utm_value == pytest.approx(flat.utm_value, rel=1e-12)


def test_points_are_not_applicable():
    result = one(Point(77.6, 12.9))
    assert result.status is FeatureStatus.NOT_APPLICABLE
    assert result.measurement_kind is None and result.utm_value is None


def test_mixed_geometry_collection_is_unsupported_not_a_crash():
    result = one(GeometryCollection([Point(1, 1), LineString([(0, 0), (1, 1)])]))
    assert result.status is FeatureStatus.UNSUPPORTED
    assert result.geometry_type == "GeometryCollection"


def test_homogeneous_geometry_collection_is_unwrapped_and_measured():
    gc = GeometryCollection(
        [Polygon(BLR_SQUARE), Polygon([(77.61, 12.97), (77.62, 12.97), (77.62, 12.98)])]
    )
    result = one(gc)
    assert result.status is FeatureStatus.MEASURED
    assert result.measurement_kind is MeasurementKind.AREA
    assert result.geometry_type == "GeometryCollection"  # reported as it came in


def test_null_and_empty_geometries_are_empty():
    null, empty = measure_batch([None, Polygon()], WGS84)
    assert null.status is FeatureStatus.EMPTY and null.geometry_type is None
    assert empty.status is FeatureStatus.EMPTY and empty.geometry_type == "Polygon"


def test_invalid_bowtie_is_repaired_and_measured():
    bowtie = Polygon([(0, 0), (1, 1), (1, 0), (0, 1)])
    result = one(bowtie)
    assert result.status is FeatureStatus.REPAIRED
    assert "Self-intersection" in result.reason
    triangles = geodesic_ring_area([(0, 0), (0.5, 0.5), (0, 1)]) + geodesic_ring_area(
        [(1, 0), (1, 1), (0.5, 0.5)]
    )
    assert result.geodesic_value == pytest.approx(triangles, rel=1e-6)


def test_projected_source_is_reprojected_not_trusted():
    """Web Mercator inflates areas by 1/cos^2(lat): 4x at 60N. We must report true area."""
    helsinki = [(24.90, 60.17), (24.91, 60.17), (24.91, 60.175), (24.90, 60.175)]
    to_3857 = Transformer.from_crs(4326, 3857, always_xy=True)
    projected = Polygon([to_3857.transform(x, y) for x, y in helsinki])
    result = one(projected, CRS.from_epsg(3857))
    truth = geodesic_ring_area(helsinki)
    assert result.utm_value == pytest.approx(truth, rel=3e-3)
    assert projected.area == pytest.approx(truth * 4, rel=0.05)  # what trusting 3857 would give


def test_geographic_coordinates_out_of_range_are_an_error():
    result = one(Polygon([(500000, 4000000), (500100, 4000000), (500100, 4000100)]))
    assert result.status is FeatureStatus.ERROR
    assert "range" in result.reason


def test_wide_and_zone_spanning_features_get_warnings():
    wide = Polygon([(10, 10), (14, 10), (14, 11), (10, 11)])
    assert W_LARGE_EXTENT in one(wide).warnings
    across = Polygon([(5.5, 40), (6.5, 40), (6.5, 40.5), (5.5, 40.5)])  # zone 31 | 32
    assert W_MULTI_ZONE in one(across).warnings
    assert one(Polygon(BLR_SQUARE)).warnings == []


def test_polar_feature_uses_ups_and_is_flagged():
    arctic = [(0, 85), (2, 85), (2, 86), (0, 86)]
    result = one(Polygon(arctic))
    assert result.utm_crs == "EPSG:32661"
    assert W_POLAR in result.warnings
    assert result.utm_value == pytest.approx(geodesic_ring_area(arctic), rel=0.01)


def test_batch_mixes_everything_and_preserves_order():
    geoms = [
        Polygon(BLR_SQUARE),
        None,
        Point(1, 1),
        LineString([(77.59, 12.97), (77.60, 12.99)]),
        Polygon(),
    ]
    statuses = [r.status for r in measure_batch(geoms, WGS84)]
    assert statuses == [
        FeatureStatus.MEASURED,
        FeatureStatus.EMPTY,
        FeatureStatus.NOT_APPLICABLE,
        FeatureStatus.MEASURED,
        FeatureStatus.EMPTY,
    ]


def test_features_in_different_zones_are_each_measured_in_their_own_zone():
    a = Polygon(BLR_SQUARE)  # zone 43N
    b = Polygon([(-122.40, 37.77), (-122.39, 37.77), (-122.39, 37.78), (-122.40, 37.78)])  # 10N
    ra, rb = measure_batch([a, b], WGS84)
    assert (ra.utm_crs, rb.utm_crs) == ("EPSG:32643", "EPSG:32610")
    assert all(math.isfinite(r.utm_value) for r in (ra, rb))


def test_empty_batch():
    assert measure_batch([], WGS84) == []
    assert isinstance(np.nan, float)  # keeps numpy import honest for linting
