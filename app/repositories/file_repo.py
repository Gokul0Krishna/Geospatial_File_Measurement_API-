"""All SQL touching the ``files`` table.

State transitions are single conditional ``UPDATE ... WHERE status IN (...)``
statements: the database arbitrates races between API processes, workers and the
sweeper, and the returned row count tells the caller whether it won.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import utcnow
from app.models.uploaded_file import UploadedFile
from app.schemas.enums import FileStatus

ACTIVE = (FileStatus.PENDING.value, FileStatus.PROCESSING.value)


class FileRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, record: UploadedFile) -> None:
        self.session.add(record)
        await self.session.flush()

    async def get(self, file_id: str) -> UploadedFile | None:
        return await self.session.get(UploadedFile, file_id)

    async def get_visible(self, file_id: str) -> UploadedFile | None:
        """Like ``get`` but files being deleted are invisible to API clients."""
        record = await self.get(file_id)
        if record is None or record.status == FileStatus.DELETING.value:
            return None
        return record

    async def list_page(
        self, *, limit: int, before_id: str | None, status: FileStatus | None
    ) -> list[UploadedFile]:
        stmt = select(UploadedFile).where(UploadedFile.status != FileStatus.DELETING.value)
        if status is not None:
            stmt = stmt.where(UploadedFile.status == status.value)
        if before_id is not None:
            stmt = stmt.where(UploadedFile.id < before_id)
        stmt = stmt.order_by(UploadedFile.id.desc()).limit(limit)
        return list((await self.session.execute(stmt)).scalars())

    async def count_active(self, client_id: str | None = None) -> int:
        stmt = select(func.count()).select_from(UploadedFile).where(UploadedFile.status.in_(ACTIVE))
        if client_id is not None:
            stmt = stmt.where(UploadedFile.client_id == client_id)
        return int((await self.session.execute(stmt)).scalar_one())

    # -- state transitions ---------------------------------------------------------

    async def claim(self, file_id: str, *, allow_reclaim: bool) -> bool:
        """PENDING -> PROCESSING (or PROCESSING -> PROCESSING when a worker retries)."""
        allowed = [FileStatus.PENDING.value]
        if allow_reclaim:
            allowed.append(FileStatus.PROCESSING.value)
        result = await self.session.execute(
            update(UploadedFile)
            .where(UploadedFile.id == file_id, UploadedFile.status.in_(allowed))
            .values(
                status=FileStatus.PROCESSING.value,
                started_at=utcnow(),
                attempts=UploadedFile.attempts + 1,
                processed_features=0,
                error=None,
            )
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def update_progress(self, file_id: str, processed: int) -> bool:
        """Also the cancellation check: false means the file left PROCESSING."""
        result = await self.session.execute(
            update(UploadedFile)
            .where(UploadedFile.id == file_id, UploadedFile.status == FileStatus.PROCESSING.value)
            .values(processed_features=processed)
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def complete(
        self,
        file_id: str,
        *,
        feature_count: int,
        crs: str,
        warnings: list[str],
        stats: dict[str, Any],
    ) -> bool:
        result = await self.session.execute(
            update(UploadedFile)
            .where(UploadedFile.id == file_id, UploadedFile.status == FileStatus.PROCESSING.value)
            .values(
                status=FileStatus.COMPLETED.value,
                feature_count=feature_count,
                processed_features=feature_count,
                crs=crs,
                warnings=warnings,
                stats=stats,
                error=None,
                completed_at=utcnow(),
            )
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def fail(
        self,
        file_id: str,
        error: str,
        *,
        from_statuses: Iterable[str] = ACTIVE,
    ) -> bool:
        result = await self.session.execute(
            update(UploadedFile)
            .where(UploadedFile.id == file_id, UploadedFile.status.in_(list(from_statuses)))
            .values(status=FileStatus.FAILED.value, error=error[:2000], completed_at=utcnow())
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def requeue_failed(self, file_id: str) -> bool:
        result = await self.session.execute(
            update(UploadedFile)
            .where(UploadedFile.id == file_id, UploadedFile.status == FileStatus.FAILED.value)
            .values(
                status=FileStatus.PENDING.value,
                error=None,
                completed_at=None,
                started_at=None,
                feature_count=None,
                processed_features=0,
                stats=None,
            )
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def mark_deleting(self, file_id: str) -> str | None:
        """Returns the status the file had when it was marked, or None if not found /
        already deleting.  Conditional on the observed status so a concurrent transition
        cannot be lost."""
        for _ in range(3):
            current = await self.session.scalar(
                select(UploadedFile.status).where(UploadedFile.id == file_id)
            )
            if current is None or current == FileStatus.DELETING.value:
                return None
            result = await self.session.execute(
                update(UploadedFile)
                .where(UploadedFile.id == file_id, UploadedFile.status == current)
                .values(status=FileStatus.DELETING.value)
            )
            if result.rowcount:  # type: ignore[attr-defined]
                return str(current)
        return None

    async def delete_row(self, file_id: str) -> None:
        await self.session.execute(delete(UploadedFile).where(UploadedFile.id == file_id))

    # -- sweeper queries -----------------------------------------------------------

    async def ids_in_status_since(
        self, status: FileStatus, *, column: str, older_than: datetime, limit: int = 500
    ) -> list[str]:
        col = getattr(UploadedFile, column)
        stmt = (
            select(UploadedFile.id)
            .where(UploadedFile.status == status.value, col < older_than)
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def existing_ids(self, ids: list[str]) -> set[str]:
        if not ids:
            return set()
        stmt = select(UploadedFile.id).where(UploadedFile.id.in_(ids))
        return set((await self.session.execute(stmt)).scalars())
