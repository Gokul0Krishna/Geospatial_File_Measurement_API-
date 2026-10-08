"""Delete, retry and housekeeping (the sweeper).

Delete is soft: the file is flipped to DELETING (invisible to clients), a running
worker notices at its next batch boundary and purges it itself, and anything that
slips through is collected by the sweeper after a grace period.  Nothing ever unlinks
a blob synchronously while a worker might still be reading it.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import utcnow
from app.core.config import Settings
from app.core.exceptions import FileNotFound, InvalidState, ServiceBusy
from app.models.uploaded_file import UploadedFile
from app.repositories.file_repo import FileRepository
from app.schemas.enums import FileStatus
from app.services.purge import purge_file
from app.storage.base import Storage
from app.workers.dispatcher import JobDispatcher

logger = logging.getLogger(__name__)
_ORPHAN_CHECK_CHUNK = 500


class LifecycleService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        storage: Storage,
        settings: Settings,
        dispatcher: JobDispatcher | None = None,
    ) -> None:
        self._sm = sessionmaker
        self._storage = storage
        self._settings = settings
        self._dispatcher = dispatcher

    # ------------------------------------------------------------------ delete

    async def request_delete(self, file_id: str) -> str:
        """Mark DELETING. Returns the status the file had. Raises FileNotFound."""
        async with self._sm() as session:
            previous = await FileRepository(session).mark_deleting(file_id)
            await session.commit()
        if previous is None:
            raise FileNotFound(f"File {file_id} does not exist.")
        return previous

    async def finish_delete(self, file_id: str, previous_status: str) -> None:
        """Background step after ``request_delete``. A PROCESSING file is purged by its
        worker (or by the sweeper as a safety net); everything else is idle."""
        if previous_status == FileStatus.PROCESSING.value:
            return
        try:
            await purge_file(self._sm, self._storage, file_id)
        except Exception:
            logger.exception("purge failed; sweeper will retry", extra={"file_id": file_id})

    # ------------------------------------------------------------------ retry

    async def request_retry(self, file_id: str) -> UploadedFile:
        if self._dispatcher is None:
            raise RuntimeError("LifecycleService was built without a dispatcher")
        async with self._sm() as session:
            repo = FileRepository(session)
            requeued = await repo.requeue_failed(file_id)
            await session.commit()
            if not requeued:
                record = await repo.get_visible(file_id)
                if record is None:
                    raise FileNotFound(f"File {file_id} does not exist.")
                raise InvalidState(
                    f"Only FAILED files can be retried (current status: {record.status})."
                )
        try:
            await self._dispatcher.enqueue(file_id)
        except Exception as exc:
            logger.exception("re-enqueue failed", extra={"file_id": file_id})
            async with self._sm() as session:
                await FileRepository(session).fail(file_id, "Could not be re-queued; retry again.")
                await session.commit()
            raise ServiceBusy("The file could not be queued; please retry shortly.") from exc
        async with self._sm() as session:
            record = await FileRepository(session).get(file_id)
        assert record is not None
        return record

    # ------------------------------------------------------------------ sweeper

    async def run_sweep(self) -> dict[str, int]:
        """Idempotent housekeeping; safe to run from many processes at once."""
        settings = self._settings
        now = utcnow()
        report: dict[str, int] = {}

        report["temp_removed"] = await self._storage.purge_stale_temp(settings.STALE_TMP_AGE_S)

        async with self._sm() as session:
            repo = FileRepository(session)
            stale_pending = await repo.ids_in_status_since(
                FileStatus.PENDING,
                column="updated_at",
                older_than=now - timedelta(seconds=settings.STALE_PENDING_S),
            )
            stale_processing = await repo.ids_in_status_since(
                FileStatus.PROCESSING,
                column="started_at",
                older_than=now - timedelta(seconds=settings.STALE_PROCESSING_S),
            )
            for file_id in stale_pending:
                await repo.fail(
                    file_id,
                    "Processing never started (the job was lost). Retry the file.",
                    from_statuses=[FileStatus.PENDING.value],
                )
            for file_id in stale_processing:
                await repo.fail(
                    file_id,
                    "Processing timed out or the worker was lost. Retry the file.",
                    from_statuses=[FileStatus.PROCESSING.value],
                )
            deleting = await repo.ids_in_status_since(
                FileStatus.DELETING,
                column="updated_at",
                older_than=now - timedelta(seconds=settings.DELETE_GRACE_S),
            )
            expired_failed = await repo.ids_in_status_since(
                FileStatus.FAILED,
                column="updated_at",
                older_than=now - timedelta(seconds=settings.FAILED_RETENTION_S),
            )
            await session.commit()
        report["stale_pending_failed"] = len(stale_pending)
        report["stale_processing_failed"] = len(stale_processing)

        purged = 0
        for file_id in deleting:
            await purge_file(self._sm, self._storage, file_id)
            purged += 1
        for file_id in expired_failed:
            async with self._sm() as session:  # claim via DELETING so a concurrent retry can't race
                previous = await FileRepository(session).mark_deleting(file_id)
                await session.commit()
            if previous == FileStatus.FAILED.value:
                await purge_file(self._sm, self._storage, file_id)
                purged += 1
        report["purged"] = purged

        report["orphan_blobs_removed"] = await self._remove_orphan_blobs(now)
        if any(report.values()):
            logger.info("sweep finished", extra={"report": report})
        return report

    async def _remove_orphan_blobs(self, now) -> int:  # type: ignore[no-untyped-def]
        cutoff = now - timedelta(seconds=self._settings.ORPHAN_BLOB_GRACE_S)
        candidates = [o.file_id for o in await self._storage.list_stored() if o.modified_at < cutoff]
        removed = 0
        for start in range(0, len(candidates), _ORPHAN_CHECK_CHUNK):
            chunk = candidates[start : start + _ORPHAN_CHECK_CHUNK]
            async with self._sm() as session:
                known = await FileRepository(session).existing_ids(chunk)
            for file_id in chunk:
                if file_id not in known:
                    await self._storage.delete_file(file_id)
                    removed += 1
        return removed
