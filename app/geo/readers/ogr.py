"""GDAL/OGR-backed reader (via pyogrio) shared by the Shapefile and KML readers.

Features stream through Arrow record batches: one pass over the file, constant
memory, no per-batch re-scan (``skip_features`` paging would re-read from the start on
every batch).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, ClassVar

import pyarrow as pa
from pyogrio import list_layers, read_info
from pyogrio.errors import DataLayerError, DataSourceError, FeatureError, GeometryError
from pyogrio.raw import open_arrow
from pyproj import CRS

from app.geo.crs import parse_crs
from app.geo.readers.base import FeatureReader, RawBatch, SourceInfo, SourceReadError, json_safe

_READ_ERRORS = (
    DataSourceError,
    DataLayerError,
    FeatureError,
    GeometryError,
    pa.ArrowException,
    ValueError,
    OSError,
    RuntimeError,
)
_DEFAULT_GEOMETRY_COLUMN = "wkb_geometry"


class OgrReader(FeatureReader):
    driver: ClassVar[str] = "OGR"

    # -- hooks ---------------------------------------------------------------------

    def resolve_crs(self, detected: CRS | None) -> CRS | None:
        return detected

    def clean_properties(self, properties: dict[str, Any]) -> dict[str, Any]:
        return {key: json_safe(value) for key, value in properties.items()}

    # -- implementation ------------------------------------------------------------

    def _layer_names(self) -> list[str]:
        try:
            layers = list_layers(self.path)
        except _READ_ERRORS as exc:
            raise SourceReadError(f"The file could not be opened: {exc}") from exc
        names = [str(row[0]) for row in layers]
        if not names:
            raise SourceReadError("The file contains no readable layers.")
        return names

    def inspect(self) -> SourceInfo:
        names = self._layer_names()
        total = 0
        count_known = True
        detected: CRS | None = None
        for name in names:
            try:
                info = read_info(self.path, layer=name)
            except _READ_ERRORS as exc:
                raise SourceReadError(f"Layer {name!r} could not be read: {exc}") from exc
            count = info.get("features")
            if count is None or count < 0:
                count_known = False
            else:
                total += int(count)
            if detected is None:
                detected = parse_crs(info.get("crs"))
        return SourceInfo(
            crs=self.resolve_crs(detected),
            feature_count=total if count_known else None,
            layers=tuple(names),
            driver=self.driver,
        )

    def iter_batches(self, batch_size: int) -> Iterator[RawBatch]:
        for name in self._layer_names():
            try:
                with open_arrow(
                    self.path, layer=name, batch_size=batch_size, use_pyarrow=True
                ) as source:
                    meta, reader = source
                    geometry_column = meta.get("geometry_name") or _DEFAULT_GEOMETRY_COLUMN
                    for batch in reader:
                        yield self._to_raw_batch(batch, geometry_column)
            except _READ_ERRORS as exc:
                raise SourceReadError(f"Layer {name!r} could not be read: {exc}") from exc

    def _to_raw_batch(self, batch: pa.RecordBatch, geometry_column: str) -> RawBatch:
        if geometry_column in batch.schema.names:
            wkb = batch.column(geometry_column).to_pylist()
            attributes = batch.drop_columns([geometry_column])
        else:  # attribute-only table
            wkb = [None] * batch.num_rows
            attributes = batch
        rows = attributes.to_pylist()
        return RawBatch(wkb=wkb, properties=[self.clean_properties(row) for row in rows])
