from __future__ import annotations

from typing import ClassVar

from app.geo.readers.ogr import OgrReader


class ShapefileReader(OgrReader):
    """ESRI Shapefile (extracted from a .zip). The CRS comes from the .prj file; if GDAL
    could not make sense of it the CRS is ``None`` and the caller decides what to do."""

    format_name: ClassVar[str] = "shapefile"
    driver: ClassVar[str] = "ESRI Shapefile"
