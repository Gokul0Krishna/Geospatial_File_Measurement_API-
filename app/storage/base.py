"""Storage abstraction: the API and workers never touch paths directly.

The local-disk implementation is the default.  An S3/MinIO implementation only has to
satisfy this interface; nothing else in the code base changes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class StoredObject:
    file_id: str
    modified_at: datetime


class Storage(ABC):
    @abstractmethod
    def new_temp_path(self, suffix: str = "") -> Path:
        """A unique path on local disk for staging an upload before it is committed."""

    @abstractmethod
    async def commit(self, temp_path: Path, key: str) -> None:
        """Atomically move a staged file into permanent storage under ``key``."""

    @abstractmethod
    async def fetch_to_local(self, key: str, work_dir: Path) -> Path:
        """A readable local path for ``key`` (callers must treat it as read-only)."""

    @abstractmethod
    async def delete_file(self, file_id: str) -> None:
        """Remove everything stored for a file id. Idempotent."""

    @abstractmethod
    async def list_stored(self) -> list[StoredObject]:
        """All stored file ids with their last-modified time (used to find orphans)."""

    @abstractmethod
    async def purge_stale_temp(self, max_age_s: float) -> int:
        """Delete staged uploads older than ``max_age_s``; returns how many were removed."""

    @abstractmethod
    async def check_writable(self) -> None:
        """Raise if the storage backend cannot currently be written to."""
