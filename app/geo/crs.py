"""CRS parsing, UTM zone selection and cached, thread-safe transformers.

Axis order: every transformer is built with ``always_xy=True`` so coordinates are
always (longitude, latitude) / (easting, northing).  Without it, pyproj follows the
authority definition of EPSG:4326, which is (latitude, longitude), and silently
swaps the axes.
"""

from __future__ import annotations

import threading
from functools import lru_cache
from typing import Any

import numpy as np
import shapely
from pyproj import CRS, Transformer

WGS84 = CRS.from_epsg(4326)
UPS_NORTH = 32661  # WGS 84 / UPS North (N,E)
UPS_SOUTH = 32761  # WGS 84 / UPS South (N,E)
_UPS_NORTH_MIN_LAT = 84.0
_UPS_SOUTH_MAX_LAT = -80.0


def parse_crs(value: Any) -> CRS | None:
    """Best-effort CRS parsing; returns ``None`` instead of raising."""
    if value is None or value == "":
        return None
    try:
        return CRS.from_user_input(value)
    except Exception:  # pyproj raises CRSError, but also ValueError/TypeError on junk input
        return None


def crs_label(crs: CRS) -> str:
    """Short human label, e.g. ``EPSG:4326``; falls back to the CRS name."""
    try:
        authority = crs.to_authority(min_confidence=70)
    except Exception:
        authority = None
    if authority:
        return f"{authority[0]}:{authority[1]}"
    return crs.name or "UNKNOWN"


def is_wgs84(crs: CRS) -> bool:
    try:
        return crs.to_epsg(min_confidence=70) == 4326
    except Exception:
        return False


def utm_zone(lons: Any) -> np.ndarray:
    """UTM zone number (1-60) for longitudes in degrees; wraps around the antimeridian."""
    lon = ((np.asarray(lons, dtype=float) + 180.0) % 360.0) - 180.0
    zone = np.floor((lon + 180.0) / 6.0).astype(int) + 1
    return np.clip(zone, 1, 60)


def utm_epsgs_for(lons: Any, lats: Any) -> np.ndarray:
    """EPSG codes of the projected CRS used to measure features centred at (lon, lat).

    UTM north is EPSG:326xx, UTM south is EPSG:327xx.  Beyond UTM's 84N/80S limit the
    universal polar stereographic CRSs are used instead.
    """
    lat = np.asarray(lats, dtype=float)
    zone = utm_zone(lons)
    codes = np.where(lat >= 0, 32600 + zone, 32700 + zone)
    codes = np.where(lat >= _UPS_NORTH_MIN_LAT, UPS_NORTH, codes)
    codes = np.where(lat <= _UPS_SOUTH_MAX_LAT, UPS_SOUTH, codes)
    return codes.astype(int)


def utm_epsg_for(lon: float, lat: float) -> int:
    return int(utm_epsgs_for([lon], [lat])[0])


@lru_cache(maxsize=256)
def epsg_crs(code: int) -> CRS:
    return CRS.from_epsg(code)


_local = threading.local()


def get_transformer(src: CRS, dst: CRS | int) -> Transformer:
    """Transformer cache, one per thread (pyproj transformers are not documented as
    thread-safe, and building one is expensive enough to be worth caching)."""
    cache: dict[tuple[str, str], Transformer] | None = getattr(_local, "cache", None)
    if cache is None:
        cache = _local.cache = {}
    dst_crs = epsg_crs(dst) if isinstance(dst, int) else dst
    key = (src.srs, dst_crs.srs)
    transformer = cache.get(key)
    if transformer is None:
        if len(cache) >= 128:
            cache.clear()
        transformer = Transformer.from_crs(src, dst_crs, always_xy=True)
        cache[key] = transformer
    return transformer


def transform_geometries(geoms: np.ndarray, transformer: Transformer) -> np.ndarray:
    """Reproject an object array of shapely geometries in one vectorised call.

    If the vectorised call fails, fall back to per-geometry transforms so a single bad
    geometry yields ``None`` for that feature instead of failing the whole batch.
    """

    def _apply(coords: np.ndarray) -> np.ndarray:
        x, y = transformer.transform(coords[:, 0], coords[:, 1])
        return np.column_stack((x, y))

    try:
        return shapely.transform(geoms, _apply)
    except Exception:
        out = np.empty(len(geoms), dtype=object)
        for i, geom in enumerate(geoms):
            try:
                out[i] = None if geom is None else shapely.transform(geom, _apply)
            except Exception:
                out[i] = None
        return out
