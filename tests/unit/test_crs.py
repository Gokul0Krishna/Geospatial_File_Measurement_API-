import numpy as np
from pyproj import CRS

from app.geo.crs import (
    UPS_NORTH,
    UPS_SOUTH,
    WGS84,
    crs_label,
    get_transformer,
    is_wgs84,
    parse_crs,
    utm_epsg_for,
    utm_epsgs_for,
    utm_zone,
)


def test_utm_epsg_known_locations():
    assert utm_epsg_for(77.59, 12.97) == 32643  # Bengaluru, 43N
    assert utm_epsg_for(-122.4, 37.77) == 32610  # San Francisco, 10N
    assert utm_epsg_for(151.2, -33.9) == 32756  # Sydney, 56S
    assert utm_epsg_for(-0.12, 51.5) == 32630  # London, 30N


def test_polar_regions_use_ups():
    assert utm_epsg_for(0, 89) == UPS_NORTH
    assert utm_epsg_for(10, -85) == UPS_SOUTH
    assert utm_epsg_for(10, 83.9) != UPS_NORTH  # still UTM just below 84N


def test_antimeridian_wraps_to_valid_zones():
    assert utm_epsg_for(180, 10) == 32601
    assert utm_epsg_for(-180, 10) == 32601
    assert utm_epsg_for(179.9, 10) == 32660


def test_vectorised_matches_scalar():
    lons = np.array([77.59, -122.4, 151.2])
    lats = np.array([12.97, 37.77, -33.9])
    assert list(utm_epsgs_for(lons, lats)) == [32643, 32610, 32756]
    assert list(utm_zone(lons)) == [43, 10, 56]


def test_transformer_is_xy_ordered():
    """Regression test for the classic EPSG:4326 lat/lon axis-order trap."""
    x, y = get_transformer(WGS84, 32643).transform(77.59, 12.97)  # lon, lat
    assert 750_000 < x < 800_000  # easting
    assert 1_420_000 < y < 1_450_000  # northing


def test_transformer_cache_reuses_instances_per_thread():
    assert get_transformer(WGS84, 32643) is get_transformer(WGS84, 32643)
    assert get_transformer(WGS84, 32643) is not get_transformer(WGS84, 32644)


def test_parse_and_label():
    assert parse_crs(None) is None
    assert parse_crs("not a crs") is None
    assert crs_label(CRS.from_epsg(3857)) == "EPSG:3857"
    assert is_wgs84(CRS.from_epsg(4326)) and not is_wgs84(CRS.from_epsg(4269))
