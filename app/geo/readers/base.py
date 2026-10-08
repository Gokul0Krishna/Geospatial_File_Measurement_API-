"""Reader contract shared by all input formats."""

from __future__ import annotations

import base64
import math
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar

from pyproj import CRS


class SourceReadError(Exception):
    """The file could not be opened or read by the geospatial driver."""


@dataclass(frozen=True)
class SourceInfo:
    crs: CRS | None
    feature_count: int | None  # None when the driver cannot tell without a full scan
    layers: tuple[str, ...]
    driver: str


@dataclass(slots=True)
class RawBatch:
    """A slice of features: WKB geometries (``None`` = no geometry) plus attributes."""

    wkb: list[bytes | None]
    properties: list[dict[str, Any]]


class FeatureReader(ABC):
    format_name: ClassVar[str]

    def __init__(self, path: Path) -> None:
        self.path = path

    @abstractmethod
    def inspect(self) -> SourceInfo:
        """Cheap open-check: proves the driver can read the file and reports CRS/count."""

    @abstractmethod
    def iter_batches(self, batch_size: int) -> Iterator[RawBatch]:
        """Single-pass, bounded-memory iteration over every feature in the file."""


def json_safe(value: Any) -> Any:
    """Make an attribute value storable as JSON (also on Postgres JSONB).

    NaN/Infinity are not valid JSON, NUL characters are rejected by Postgres, and
    dates / decimals / bytes have no JSON representation.
    """
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, Decimal):
        as_float = float(value)
        return as_float if math.isfinite(as_float) else None
    if isinstance(value, bytes | bytearray):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, list | tuple):
        return [json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    return str(value)
