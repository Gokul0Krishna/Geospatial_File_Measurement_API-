import time
import uuid

import pytest

from app.core.config import Settings
from app.core.exceptions import InvalidCursor, InvalidParameter
from app.core.ids import new_id
from app.core.pagination import (
    decode_id_cursor,
    decode_index_cursor,
    encode_id_cursor,
    encode_index_cursor,
    resolve_limit,
)

SETTINGS = Settings(_env_file=None)


def test_index_cursor_roundtrip():
    assert decode_index_cursor(encode_index_cursor(41)) == 41
    assert decode_index_cursor(None) == -1


def test_id_cursor_roundtrip():
    file_id = new_id()
    assert decode_id_cursor(encode_id_cursor(file_id)) == file_id
    assert decode_id_cursor(None) is None


@pytest.mark.parametrize("token", ["!!!", "e30", "W10", "eyJhZnRlciI6LTF9", "eyJhZnRlciI6dHJ1ZX0", "eyJhZnRlciI6IjEifQ"])
def test_malformed_index_cursors_rejected(token):
    with pytest.raises(InvalidCursor):
        decode_index_cursor(token)


def test_malformed_id_cursor_rejected():
    from app.core.pagination import _encode

    with pytest.raises(InvalidCursor):
        decode_id_cursor(_encode({"before": "'; DROP TABLE files;--"}))
    with pytest.raises(InvalidCursor):
        decode_id_cursor(_encode({"after": 3}))


def test_limits():
    assert resolve_limit(None, SETTINGS) == SETTINGS.PAGE_DEFAULT_LIMIT
    assert resolve_limit(1000, SETTINGS) == 1000
    assert resolve_limit(100, SETTINGS, with_geometry=True) == 100
    for bad in (0, -5, 1001):
        with pytest.raises(InvalidParameter):
            resolve_limit(bad, SETTINGS)
    with pytest.raises(InvalidParameter, match="include_geometry"):
        resolve_limit(101, SETTINGS, with_geometry=True)


def test_new_id_is_a_valid_uuid7():
    value = uuid.UUID(new_id())
    assert value.version == 7
    assert value.variant == uuid.RFC_4122


def test_ids_sort_by_creation_time_and_are_unique():
    ids = []
    for _ in range(5):
        ids.append(new_id())
        time.sleep(0.003)
    assert ids == sorted(ids)
    assert len({new_id() for _ in range(10_000)}) == 10_000


def test_sanitize_filename():
    from app.services.upload_service import sanitize_filename

    assert sanitize_filename("../../etc/pass\x01wd.kml") == "passwd.kml"
    assert sanitize_filename("C:\\Users\\me\\survey.zip") == "survey.zip"
    assert sanitize_filename(None) == "upload"
    assert sanitize_filename("\x00\x1f") == "upload"
    assert len(sanitize_filename("a" * 1000 + ".kml")) == 255
