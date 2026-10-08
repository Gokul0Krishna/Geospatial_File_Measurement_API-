"""Defensive handling of uploaded shapefile archives.

Threats covered: zip-slip (path traversal), zip bombs (declared size, per-entry
compression ratio, total ratio, entry count), duplicate member names (GDAL's behaviour
is undefined), encrypted members, and archives that do not contain a complete
shapefile.  Validation (``inspect_shapefile_zip``) is cheap and never extracts;
extraction (``extract_shapefile``) only writes the files GDAL needs, under fixed names,
so member names can never influence where bytes land on disk.
"""

from __future__ import annotations

import re
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

_COPY_CHUNK = 1024 * 1024
_REQUIRED = (".shp", ".shx", ".dbf")
_OPTIONAL = (".prj", ".cpg")
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")


class ZipValidationError(ValueError):
    """The archive is unsafe or is not a usable shapefile. Message is safe to show users."""


class MissingProjectionError(ZipValidationError):
    """Well-formed shapefile archive, but it has no .prj and strict mode is on."""


@dataclass(frozen=True)
class ZipLimits:
    max_entries: int
    max_uncompressed_bytes: int
    max_ratio: int
    ratio_min_bytes: int


@dataclass(frozen=True)
class ShapefilePlan:
    members: dict[str, zipfile.ZipInfo]  # extension (".shp") -> member
    has_prj: bool


def _normalise(name: str) -> str:
    return name.replace("\\", "/")


def _check_name(name: str) -> None:
    norm = _normalise(name)
    parts = PurePosixPath(norm).parts
    if (
        "\x00" in name
        or norm.startswith("/")
        or _WINDOWS_DRIVE.match(norm)
        or ".." in parts
    ):
        raise ZipValidationError(f"Archive contains an unsafe path: {name!r}.")


def _is_junk(name: str) -> bool:
    norm = _normalise(name)
    return norm.startswith("__MACOSX/") or PurePosixPath(norm).name.startswith("._")


def inspect_shapefile_zip(path: Path, limits: ZipLimits, *, require_prj: bool) -> ShapefilePlan:
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ZipValidationError("The file is not a valid zip archive.") from exc

    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) > limits.max_entries:
            raise ZipValidationError(
                f"Archive contains {len(infos)} files; the limit is {limits.max_entries}."
            )

        seen: set[str] = set()
        total = 0
        for info in infos:
            _check_name(info.filename)
            if info.flag_bits & 0x1:
                raise ZipValidationError("Encrypted archive members are not supported.")
            key = _normalise(info.filename).lstrip("./").lower()
            if key in seen:
                raise ZipValidationError(f"Archive contains duplicate entry {info.filename!r}.")
            seen.add(key)
            total += info.file_size
            if total > limits.max_uncompressed_bytes:
                raise ZipValidationError(
                    "Archive would expand beyond the maximum allowed uncompressed size."
                )
            if (
                info.file_size >= limits.ratio_min_bytes
                and info.file_size > limits.max_ratio * max(info.compress_size, 1)
            ):
                raise ZipValidationError(
                    f"Entry {info.filename!r} has a suspicious compression ratio "
                    f"(> {limits.max_ratio}:1)."
                )

        candidates: dict[tuple[str, str], dict[str, zipfile.ZipInfo]] = {}
        for info in infos:
            if _is_junk(info.filename):
                continue
            pure = PurePosixPath(_normalise(info.filename))
            ext = pure.suffix.lower()
            if ext in _REQUIRED or ext in _OPTIONAL:
                stem_key = (str(pure.parent).lower(), pure.stem.lower())
                candidates.setdefault(stem_key, {})[ext] = info

        shapefiles = {k: v for k, v in candidates.items() if ".shp" in v}
        if not shapefiles:
            raise ZipValidationError("Archive does not contain a .shp file.")
        if len(shapefiles) > 1:
            raise ZipValidationError(
                "Archive contains more than one shapefile; upload one shapefile per zip."
            )

        members = next(iter(shapefiles.values()))
        missing = [ext for ext in _REQUIRED if ext not in members]
        if missing:
            raise ZipValidationError(
                f"Shapefile is incomplete; missing component(s): {', '.join(missing)}."
            )
        if require_prj and ".prj" not in members:
            raise MissingProjectionError(
                "Shapefile has no .prj file, so its coordinate reference system is unknown. "
                "Include the .prj (or run the service with REQUIRE_PRJ=false to assume a CRS)."
            )
        return ShapefilePlan(members=members, has_prj=".prj" in members)


def extract_shapefile(path: Path, plan: ShapefilePlan, dest_dir: Path, limits: ZipLimits) -> Path:
    """Extract the shapefile components as ``layer.<ext>`` and return the ``.shp`` path.

    ``zipfile`` never yields more bytes than a member's declared size, so a header that
    lies cannot make us write more than ``max_uncompressed_bytes`` (validated above);
    we still count bytes as a second line of defence.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    written_total = 0
    try:
        with zipfile.ZipFile(path) as zf:
            for ext, info in plan.members.items():
                target = dest_dir / f"layer{ext}"
                with zf.open(info) as src, open(target, "wb") as dst:
                    while chunk := src.read(_COPY_CHUNK):
                        written_total += len(chunk)
                        if written_total > limits.max_uncompressed_bytes:
                            raise ZipValidationError("Archive expanded beyond the allowed size.")
                        dst.write(chunk)
    except (zipfile.BadZipFile, zlib.error, RuntimeError, EOFError, OSError) as exc:
        raise ZipValidationError(f"Archive is corrupt and could not be extracted: {exc}") from exc
    return dest_dir / "layer.shp"
