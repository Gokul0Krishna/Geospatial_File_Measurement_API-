from __future__ import annotations

from pathlib import Path

from app.geo.readers.base import FeatureReader
from app.geo.readers.kml import KmlReader
from app.geo.readers.shapefile import ShapefileReader

READERS: dict[str, type[FeatureReader]] = {
    ShapefileReader.format_name: ShapefileReader,
    KmlReader.format_name: KmlReader,
}


def get_reader(source_format: str, path: Path) -> FeatureReader:
    try:
        return READERS[source_format](path)
    except KeyError:
        raise ValueError(f"Unsupported source format: {source_format!r}") from None
