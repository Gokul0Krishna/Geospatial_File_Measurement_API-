from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    """Timezone-aware 'now' in UTC. All timestamps in the system are UTC."""
    return datetime.now(UTC)
