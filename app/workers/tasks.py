"""arq task definitions.

Retry policy
------------
* ``PermanentProcessingError`` is handled inside ``ProcessingService`` (file -> FAILED,
  no retry: a corrupt file will not get better).
* Any other exception is treated as transient (DB blip, I/O hiccup): retried with
  exponential backoff up to ``JOB_MAX_TRIES``; the last failure lands the file in FAILED
  (our dead-letter state - visible via the API and retryable by hand).
* The job deadline is enforced here (slightly under arq's own ``job_timeout``) so we can
  record a useful FAILED message instead of leaving the file stuck in PROCESSING.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from arq import Retry

from app.core.config import Settings
from app.services.lifecycle_service import LifecycleService
from app.services.processing_service import ProcessingService

logger = logging.getLogger(__name__)


async def process_file(ctx: dict[str, Any], file_id: str) -> None:
    processing: ProcessingService = ctx["processing"]
    settings: Settings = ctx["settings"]
    job_try: int = ctx.get("job_try", 1)

    deadline = asyncio.timeout(settings.JOB_TIMEOUT_S)
    try:
        async with deadline:
            await processing.process(file_id, reclaim=job_try > 1)
    except Exception as exc:
        if isinstance(exc, TimeoutError) and deadline.expired():
            logger.error("job timed out", extra={"file_id": file_id})
            await processing.mark_failed(
                file_id, f"Processing exceeded the {settings.JOB_TIMEOUT_S}s time limit."
            )
            return
        if job_try >= settings.JOB_MAX_TRIES:
            logger.exception("job failed permanently", extra={"file_id": file_id, "try": job_try})
            await processing.mark_failed(
                file_id,
                f"Processing failed after {job_try} attempts ({exc.__class__.__name__}).",
            )
            return
        delay = settings.JOB_RETRY_BASE_DELAY_S * 2 ** (job_try - 1)
        logger.warning(
            "job failed; retrying",
            extra={"file_id": file_id, "try": job_try, "retry_in_s": delay, "error": repr(exc)},
        )
        raise Retry(defer=delay) from exc


async def sweep(ctx: dict[str, Any]) -> dict[str, int]:
    lifecycle: LifecycleService = ctx["lifecycle"]
    return await lifecycle.run_sweep()
