from __future__ import annotations

from typing import Any, ClassVar

from pyproj import CRS

from app.geo.crs import WGS84
from app.geo.readers.ogr import OgrReader

# GDAL's KML driver emits these style/time columns for every layer, mostly with
# placeholder values. They are noise unless the file really set them.
_NOISE_FIELDS = {
    "tessellate",
    "extrude",
    "visibility",
    "drawOrder",
    "icon",
    "altitudeMode",
    "timestamp",
    "begin",
    "end",
}
_PLACEHOLDERS = (None, "", -1, 0)


class KmlReader(OgrReader):
    """KML. Every ``<Folder>`` becomes a GDAL layer, so we iterate all layers.

    KML coordinates are WGS84 by specification, regardless of what the driver reports.
    """

    format_name: ClassVar[str] = "kml"
    driver: ClassVar[str] = "KML"

    def resolve_crs(self, detected: CRS | None) -> CRS | None:
        return WGS84

    def clean_properties(self, properties: dict[str, Any]) -> dict[str, Any]:
        cleaned = super().clean_properties(properties)
        out: dict[str, Any] = {}
        for key, value in cleaned.items():
            if value is None:
                continue  # the KML driver unions columns across a layer; unset means absent
            if key in _NOISE_FIELDS and value in _PLACEHOLDERS:
                continue
            out["name" if key == "Name" else key] = value
        return out
