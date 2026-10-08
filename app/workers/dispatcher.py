"""How a freshly uploaded file gets processed.

``InlineDispatcher`` runs the job inside the request (no infrastructure needed);
``ArqDispatcher`` puts it on a Redis queue for ``arq`` workers.
"""

from __future__ import annotations

from typing import Protocol

from arq.connections import ArqRedis

from app.core.ids import new_id
from app.services.processing_service import ProcessingService


class JobDispatcher(Protocol):
    is_inline: bool

    async def enqueue(self, file_id: str) -> None: ...


class InlineDispatcher:
    is_inline = True

    def __init__(self, processing: ProcessingService) -> None:
        self._processing = processing

    async def enqueue(self, file_id: str) -> None:
        await self._processing.process_inline(file_id)


class ArqDispatcher:
    is_inline = False

    def __init__(self, pool: ArqRedis) -> None:
        self.pool = pool

    async def enqueue(self, file_id: str) -> None:
        # A fresh job id per dispatch: a retried file must not collide with the result
        # key of its earlier job.
        await self.pool.enqueue_job("process_file", file_id, _job_id=f"{file_id}:{new_id()[-8:]}")
