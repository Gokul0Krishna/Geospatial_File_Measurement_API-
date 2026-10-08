"""UUIDv7 identifiers.

UUIDv7 puts a millisecond timestamp in the high bits, so ids sort by creation time.
That gives us keyset pagination on the primary key alone (no extra ``created_at``
index) and keeps B-tree inserts append-mostly.  Python only ships ``uuid.uuid7`` from
3.14, so we build it by hand (RFC 9562 section 5.7).
"""

from __future__ import annotations

import os
import time
import uuid


def new_id() -> str:
    timestamp_ms = time.time_ns() // 1_000_000
    rand = int.from_bytes(os.urandom(10), "big")  # 80 random bits
    rand_a = rand >> 68  # top 12 bits
    rand_b = rand & ((1 << 62) - 1)  # low 62 bits
    value = (
        ((timestamp_ms & ((1 << 48) - 1)) << 80)
        | (0x7 << 76)  # version 7
        | (rand_a << 64)
        | (0b10 << 62)  # RFC 4122 variant
        | rand_b
    )
    return str(uuid.UUID(int=value))
