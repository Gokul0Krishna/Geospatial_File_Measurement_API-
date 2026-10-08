import os
import zipfile
from pathlib import Path

import pytest

from app.utils.zip_safety import (
    MissingProjectionError,
    ZipLimits,
    ZipValidationError,
    extract_shapefile,
    inspect_shapefile_zip,
)
from tests.helpers import BLR_SQUARE, build_shapefile_zip, build_zip

LIMITS = ZipLimits(max_entries=20, max_uncompressed_bytes=50 * 1024 * 1024, max_ratio=100, ratio_min_bytes=1024 * 1024)
PARTS = {"a.shp": b"s" * 50, "a.shx": b"x" * 50, "a.dbf": b"d" * 50, "a.prj": b"GEOGCS[]"}


def inspect(tmp_path: Path, data: bytes, *, limits: ZipLimits = LIMITS, require_prj: bool = True):
    path = tmp_path / "in.zip"
    path.write_bytes(data)
    return inspect_shapefile_zip(path, limits, require_prj=require_prj)


def test_valid_archive(tmp_path):
    plan = inspect(tmp_path, build_shapefile_zip("polygon", [[BLR_SQUARE]]))
    assert set(plan.members) == {".shp", ".shx", ".dbf", ".prj", ".cpg"}
    assert plan.has_prj


def test_not_a_zip(tmp_path):
    with pytest.raises(ZipValidationError, match="not a valid zip"):
        inspect(tmp_path, b"this is not a zip file at all")


@pytest.mark.parametrize("missing", [".shx", ".dbf", ".shp"])
def test_incomplete_shapefile(tmp_path, missing):
    entries = {k: v for k, v in PARTS.items() if not k.endswith(missing)}
    with pytest.raises(ZipValidationError):
        inspect(tmp_path, build_zip(entries))


def test_missing_prj_is_a_distinct_error_in_strict_mode(tmp_path):
    entries = {k: v for k, v in PARTS.items() if not k.endswith(".prj")}
    with pytest.raises(MissingProjectionError):
        inspect(tmp_path, build_zip(entries))
    assert inspect(tmp_path, build_zip(entries), require_prj=False).has_prj is False


def test_multiple_shapefiles_rejected(tmp_path):
    entries = {**PARTS, "b.shp": b"1", "b.shx": b"1", "b.dbf": b"1", "b.prj": b"1"}
    with pytest.raises(ZipValidationError, match="more than one"):
        inspect(tmp_path, build_zip(entries))


@pytest.mark.parametrize(
    "evil", ["../evil.shp", "/etc/passwd", "a/../../b.dbf", "C:\\windows\\x.shp", "..\\up.shx"]
)
def test_zip_slip_paths_rejected(tmp_path, evil):
    with pytest.raises(ZipValidationError, match="unsafe path"):
        inspect(tmp_path, build_zip({**PARTS, evil: b"x"}))


def test_duplicate_entries_rejected_case_insensitively(tmp_path):
    data = build_zip(PARTS, allow_duplicates=[("A.SHP", b"other")])
    with pytest.raises(ZipValidationError, match="duplicate"):
        inspect(tmp_path, data)


def test_too_many_entries(tmp_path):
    entries = {**PARTS, **{f"junk{i}.txt": b"x" for i in range(30)}}
    with pytest.raises(ZipValidationError, match="limit"):
        inspect(tmp_path, build_zip(entries))


def test_compression_ratio_bomb_rejected(tmp_path):
    bomb = {**PARTS, "filler.bin": b"\x00" * (8 * 1024 * 1024)}  # 8 MB -> a few KB
    with pytest.raises(ZipValidationError, match="compression ratio"):
        inspect(tmp_path, build_zip(bomb))


def test_total_uncompressed_size_cap(tmp_path):
    small = ZipLimits(max_entries=20, max_uncompressed_bytes=100_000, max_ratio=100, ratio_min_bytes=1 << 20)
    entries = {**PARTS, "noise.bin": os.urandom(200_000)}
    with pytest.raises(ZipValidationError, match="uncompressed size"):
        inspect(tmp_path, build_zip(entries), limits=small)


def test_macos_junk_and_nested_directories_are_handled(tmp_path):
    entries = {f"data/{k}": v for k, v in PARTS.items()}
    entries["__MACOSX/data/._a.shp"] = b"junk"
    plan = inspect(tmp_path, build_zip(entries))
    assert plan.members[".shp"].filename == "data/a.shp"


def test_extraction_uses_fixed_names_and_stays_inside_dest(tmp_path):
    path = tmp_path / "in.zip"
    path.write_bytes(build_zip({f"deep/dir/{k.upper()}": v for k, v in PARTS.items()}))
    plan = inspect_shapefile_zip(path, LIMITS, require_prj=True)
    dest = tmp_path / "out"
    shp = extract_shapefile(path, plan, dest, LIMITS)
    assert shp == dest / "layer.shp"
    assert sorted(p.name for p in dest.iterdir()) == ["layer.dbf", "layer.prj", "layer.shp", "layer.shx"]
    assert not (tmp_path / "deep").exists()


def test_extraction_of_truncated_archive_fails_cleanly(tmp_path):
    good = build_shapefile_zip("polygon", [[BLR_SQUARE]])
    path = tmp_path / "in.zip"
    path.write_bytes(good)
    plan = inspect_shapefile_zip(path, LIMITS, require_prj=True)
    # corrupt the stored bytes of one member after validation
    corrupted = bytearray(good)
    shp_info = plan.members[".shp"]
    corrupted[shp_info.header_offset + 40] ^= 0xFF
    path.write_bytes(bytes(corrupted))
    with pytest.raises(ZipValidationError):
        extract_shapefile(path, plan, tmp_path / "out", LIMITS)
    assert zipfile.is_zipfile(path)
