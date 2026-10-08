from __future__ import annotations

import asyncio
import os
import shutil
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from app.storage.base import Storage, StoredObject


class LocalStorage(Storage):
    """Files live under ``<root>/files/<file_id>/``; staging happens in ``<root>/tmp``
    (same filesystem as the destination, so the final move is an atomic rename)."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.files_dir = self.root / "files"
        self.tmp_dir = self.root / "tmp"
        self.work_dir = self.root / "work"
        for directory in (self.files_dir, self.tmp_dir, self.work_dir):
            directory.mkdir(parents=True, exist_ok=True)

    def _resolve(self, key: str) -> Path:
        path = (self.files_dir / key).resolve()
        if not path.is_relative_to(self.files_dir):
            raise ValueError("storage key escapes the storage root")
        return path

    def new_temp_path(self, suffix: str = "") -> Path:
        return self.tmp_dir / f"{uuid.uuid4().hex}{suffix}.part"

    async def commit(self, temp_path: Path, key: str) -> None:
        destination = self._resolve(key)

        def _move() -> None:
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(temp_path, destination)

        await asyncio.to_thread(_move)

    async def fetch_to_local(self, key: str, work_dir: Path) -> Path:
        path = self._resolve(key)
        if not await asyncio.to_thread(path.is_file):
            raise FileNotFoundError(f"stored object {key!r} is missing")
        return path

    async def delete_file(self, file_id: str) -> None:
        directory = self._resolve(file_id)
        await asyncio.to_thread(shutil.rmtree, directory, True)

    async def list_stored(self) -> list[StoredObject]:
        def _scan() -> list[StoredObject]:
            out: list[StoredObject] = []
            with os.scandir(self.files_dir) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        mtime = datetime.fromtimestamp(entry.stat().st_mtime, UTC)
                        out.append(StoredObject(file_id=entry.name, modified_at=mtime))
            return out

        return await asyncio.to_thread(_scan)

    async def purge_stale_temp(self, max_age_s: float) -> int:
        def _purge() -> int:
            removed = 0
            cutoff = time.time() - max_age_s
            for directory in (self.tmp_dir, self.work_dir):
                with os.scandir(directory) as entries:
                    for entry in entries:
                        try:
                            if entry.stat(follow_symlinks=False).st_mtime < cutoff:
                                if entry.is_dir(follow_symlinks=False):
                                    shutil.rmtree(entry.path, ignore_errors=True)
                                else:
                                    os.unlink(entry.path)
                                removed += 1
                        except FileNotFoundError:
                            continue
            return removed

        return await asyncio.to_thread(_purge)

    async def check_writable(self) -> None:
        def _probe() -> None:
            probe = self.tmp_dir / f".ready-{uuid.uuid4().hex}"
            probe.write_bytes(b"ok")
            probe.unlink()

        await asyncio.to_thread(_probe)
