"""Opaque keyset-pagination cursors and limit resolution."""

from __future__ import annotations

import base64
import binascii
import json
import re

from app.core.config import Settings
from app.core.exceptions import InvalidCursor, InvalidParameter

_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _encode(payload: dict[str, object]) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode(token: str) -> dict[str, object]:
    try:
        padded = token + "=" * (-len(token) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise InvalidCursor("The pagination cursor is malformed.") from exc
    if not isinstance(data, dict):
        raise InvalidCursor("The pagination cursor is malformed.")
    return data


def encode_index_cursor(last_index: int) -> str:
    return _encode({"after": last_index})


def decode_index_cursor(token: str | None) -> int:
    """Returns the feature index to continue *after* (-1 means 'from the start')."""
    if token is None:
        return -1
    value = _decode(token).get("after")
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise InvalidCursor("The pagination cursor is malformed.")
    return value


def encode_id_cursor(last_id: str) -> str:
    return _encode({"before": last_id})


def decode_id_cursor(token: str | None) -> str | None:
    if token is None:
        return None
    value = _decode(token).get("before")
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise InvalidCursor("The pagination cursor is malformed.")
    return value


def resolve_limit(limit: int | None, settings: Settings, *, with_geometry: bool = False) -> int:
    cap = settings.PAGE_MAX_LIMIT_WITH_GEOMETRY if with_geometry else settings.PAGE_MAX_LIMIT
    if limit is None:
        return min(settings.PAGE_DEFAULT_LIMIT, cap)
    if limit < 1 or limit > cap:
        suffix = " when include_geometry=true" if with_geometry else ""
        raise InvalidParameter(f"limit must be between 1 and {cap}{suffix}.")
    return limit
