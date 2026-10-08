"""read -> measure -> persist, with retry-safe semantics.

Idempotency / crash safety
--------------------------
* The first thing a run does is claim the file with a conditional UPDATE and wipe any
  feature rows left by a previous attempt ("wipe and redo").
* Features are inserted batch by batch, each batch in one transaction together with the
  progress update.  Partially written rows are invisible: the API only serves features
  of COMPLETED files.
* COMPLETED is set in a single final UPDATE.  Re-running a COMPLETED file is a no-op
  because the claim only succeeds from PENDING (or PROCESSING when a worker retries).
* The per-batch progress UPDATE doubles as the cancellation check: it only matches
  while the file is still PROCESSING, so a concurrent delete/timeout stops the run at
  the next batch boundary.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import shutil
import tempfile
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

import numpy as np
import shapely
from pyproj import CRS
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.geo.crs import crs_label, parse_crs
from app.geo.measure import measure_batch
from app.geo.readers.base import FeatureReader, RawBatch, SourceInfo, SourceReadError
from app.geo.readers.registry import get_reader
from app.geo.types import FeatureResult, FeatureStatus, MeasurementKind
from app.models.uploaded_file import UploadedFile
from app.repositories.feature_repo import FeatureRepository
from app.repositories.file_repo import FileRepository
from app.schemas.enums import FileStatus, SourceFormat
from app.services.purge import purge_file
from app.storage.base import Storage
from app.utils.zip_safety import (
    ZipLimits,
    ZipValidationError,
    extract_shapefile,
    inspect_shapefile_zip,
)

logger = logging.getLogger(__name__)
T = TypeVar("T")


class PermanentProcessingError(Exception):
    """Retrying cannot help (bad file, budget exceeded, unknown CRS...). The message is
    user-facing and is stored on the file as ``error``."""


class _ThreadRunner:
    """Runs blocking GDAL / shapely / pyproj work on ONE dedicated thread per job.

    A single thread keeps the GDAL dataset and the generator that wraps it on the same
    thread for the whole job, keeps the (thread-local) pyproj transformer cache warm,
    and never blocks the event loop.
    """

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="geo-job")

    async def run(self, fn: Callable[..., T], *args: Any) -> T:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, functools.partial(fn, *args))

    def submit(self, fn: Callable[..., Any], *args: Any) -> None:
        self._executor.submit(fn, *args)

    def close(self) -> None:
        self._executor.shutdown(wait=False)


@dataclass
class _Stats:
    by_status: Counter[str] = field(default_factory=Counter)
    by_geometry_type: Counter[str] = field(default_factory=Counter)
    area_m2: dict[str, float] = field(default_factory=lambda: {"utm": 0.0, "geodesic": 0.0})
    length_m: dict[str, float] = field(default_factory=lambda: {"utm": 0.0, "geodesic": 0.0})

    def add(self, result: FeatureResult) -> None:
        self.by_status[result.status.value] += 1
        if result.geometry_type:
            self.by_geometry_type[result.geometry_type] += 1
        if result.measurement_kind is None:
            return
        target = self.area_m2 if result.measurement_kind is MeasurementKind.AREA else self.length_m
        if result.utm_value is not None:
            target["utm"] += result.utm_value
        if result.geodesic_value is not None:
            target["geodesic"] += result.geodesic_value

    def as_dict(self) -> dict[str, Any]:
        return {
            "by_status": dict(self.by_status),
            "by_geometry_type": dict(self.by_geometry_type),
            "total_area_m2": self.area_m2,
            "total_length_m": self.length_m,
        }


class ProcessingService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        storage: Storage,
        settings: Settings,
    ) -> None:
        self._sm = sessionmaker
        self._storage = storage
        self._settings = settings
        self._zip_limits = ZipLimits(
            max_entries=settings.MAX_ZIP_ENTRIES,
            max_uncompressed_bytes=settings.MAX_ZIP_UNCOMPRESSED_BYTES,
            max_ratio=settings.MAX_ZIP_COMPRESSION_RATIO,
            ratio_min_bytes=settings.ZIP_RATIO_MIN_BYTES,
        )

    # ------------------------------------------------------------------ entry points

    async def process(self, file_id: str, *, reclaim: bool = False) -> None:
        """Process one file. Permanent problems end in FAILED; any other exception
        propagates so the caller (arq task) can retry. ``reclaim`` allows taking over a
        file that is already PROCESSING (a retry after a crashed attempt)."""
        async with self._sm() as session:
            repo = FileRepository(session)
            claimed = await repo.claim(file_id, allow_reclaim=reclaim)
            await session.commit()
            record = await repo.get(file_id) if claimed else None
        if record is None:
            logger.info("process skipped (not claimable)", extra={"file_id": file_id})
            return

        try:
            await self._run(record)
        except PermanentProcessingError as exc:
            logger.warning("processing failed", extra={"file_id": file_id, "error": str(exc)})
            await self.mark_failed(file_id, str(exc))

    async def process_inline(self, file_id: str) -> None:
        """Single attempt, never raises: used when there is no queue to retry from."""
        try:
            await self.process(file_id)
        except Exception as exc:
            logger.exception("processing crashed", extra={"file_id": file_id})
            await self.mark_failed(
                file_id, f"Internal error while processing ({exc.__class__.__name__})."
            )

    async def mark_failed(self, file_id: str, error: str) -> None:
        async with self._sm() as session:
            failed = await FileRepository(session).fail(file_id, error)
            if failed:  # never touch the features of a file that someone else completed
                await FeatureRepository(session).delete_for_file(file_id)
            await session.commit()

    # ------------------------------------------------------------------ the job itself

    async def _run(self, record: UploadedFile) -> None:
        settings = self._settings
        file_id = record.id
        runner = _ThreadRunner()
        workdir = Path(await asyncio.to_thread(tempfile.mkdtemp, dir=settings.work_dir))
        batches = None
        try:
            async with self._sm() as session:  # wipe leftovers from a crashed attempt
                await FeatureRepository(session).delete_for_file(file_id)
                await session.commit()

            try:
                local = await self._storage.fetch_to_local(record.storage_key, workdir)
            except FileNotFoundError as exc:
                raise PermanentProcessingError("The stored upload is missing.") from exc

            # Cheap open-check first: a bad file fails in seconds, not after minutes.
            try:
                reader, info = await runner.run(self._open, record.source_format, local, workdir)
            except (ZipValidationError, SourceReadError) as exc:
                raise PermanentProcessingError(str(exc)) from exc

            crs, file_warnings = self._resolve_crs(info)
            if info.feature_count is not None and info.feature_count > settings.MAX_FEATURES:
                raise PermanentProcessingError(
                    f"File has {info.feature_count} features; the limit is {settings.MAX_FEATURES}."
                )

            batches = reader.iter_batches(settings.READ_BATCH_SIZE)
            stats = _Stats()
            index = 0
            vertices = 0
            while True:
                try:
                    batch = await runner.run(next, batches, None)
                except SourceReadError as exc:
                    raise PermanentProcessingError(str(exc)) from exc
                if batch is None:
                    break

                rows, results, batch_vertices = await runner.run(
                    self._measure, batch, index, crs, file_id
                )
                index += len(rows)
                vertices += batch_vertices
                if index > settings.MAX_FEATURES:
                    raise PermanentProcessingError(
                        f"File has more than {settings.MAX_FEATURES} features."
                    )
                if vertices > settings.MAX_VERTICES:
                    raise PermanentProcessingError(
                        f"File has more than {settings.MAX_VERTICES} vertices."
                    )

                async with self._sm() as session:
                    alive = await FileRepository(session).update_progress(file_id, index)
                    if alive:
                        await FeatureRepository(session).bulk_insert(rows)
                    await session.commit()
                if not alive:
                    await self._handle_lost_claim(file_id)
                    return
                for result in results:
                    stats.add(result)

            if index == 0:
                file_warnings.append("NO_FEATURES")
            async with self._sm() as session:
                done = await FileRepository(session).complete(
                    file_id,
                    feature_count=index,
                    crs=crs_label(crs),
                    warnings=file_warnings,
                    stats=stats.as_dict(),
                )
                await session.commit()
            if not done:
                await self._handle_lost_claim(file_id)
                return
            logger.info(
                "file processed",
                extra={"file_id": file_id, "features": index, "vertices": vertices},
            )
        finally:
            if batches is not None:
                runner.submit(batches.close)
            runner.close()
            await asyncio.to_thread(shutil.rmtree, workdir, True)

    # ------------------------------------------------------------------ helpers

    def _open(
        self, source_format: str, local: Path, workdir: Path
    ) -> tuple[FeatureReader, SourceInfo]:
        path = local
        if source_format == SourceFormat.SHAPEFILE.value:
            plan = inspect_shapefile_zip(
                local, self._zip_limits, require_prj=self._settings.REQUIRE_PRJ
            )
            path = extract_shapefile(local, plan, workdir / "src", self._zip_limits)
        reader = get_reader(source_format, path)
        return reader, reader.inspect()

    def _resolve_crs(self, info: SourceInfo) -> tuple[CRS, list[str]]:
        if info.crs is not None:
            return info.crs, []
        if self._settings.REQUIRE_PRJ:
            raise PermanentProcessingError(
                "The coordinate reference system could not be determined from the file "
                "(the .prj is missing or unreadable)."
            )
        assumed = parse_crs(self._settings.ASSUMED_CRS)
        if assumed is None:
            raise PermanentProcessingError("The service's ASSUMED_CRS setting is not a valid CRS.")
        return assumed, [f"CRS_ASSUMED:{crs_label(assumed)}"]

    def _measure(
        self, batch: RawBatch, start_index: int, crs: CRS, file_id: str
    ) -> tuple[list[dict[str, Any]], list[FeatureResult], int]:
        count = len(batch.wkb)
        wkb = np.empty(count, dtype=object)
        for i, value in enumerate(batch.wkb):
            wkb[i] = value
        decoded = shapely.from_wkb(wkb, on_invalid="ignore")
        vertices = int(np.nansum(shapely.get_num_coordinates(decoded)))

        results = measure_batch(list(decoded), crs, long_extent_deg=self._settings.LONG_EXTENT_DEGREES)
        for i in range(count):
            if batch.wkb[i] is not None and decoded[i] is None:
                results[i] = FeatureResult(
                    FeatureStatus.ERROR, None, reason="Geometry data could not be decoded."
                )

        rows: list[dict[str, Any]] = []
        for i, result in enumerate(results):
            rows.append(
                {
                    "file_id": file_id,
                    "feature_index": start_index + i,
                    "geometry_type": result.geometry_type,
                    "geometry_wkb": bytes(batch.wkb[i]) if batch.wkb[i] is not None else None,
                    "properties": batch.properties[i],
                    "status": result.status.value,
                    "reason": result.reason,
                    "measurement_kind": result.measurement_kind.value
                    if result.measurement_kind
                    else None,
                    "utm_crs": result.utm_crs,
                    "utm_value": result.utm_value,
                    "geodesic_value": result.geodesic_value,
                    "warnings": result.warnings,
                }
            )
        return rows, results, vertices

    async def _handle_lost_claim(self, file_id: str) -> None:
        """The file left PROCESSING while we worked on it. If it was deleted we finish the
        job (purge); anything else (e.g. the sweeper failed it) is left alone."""
        async with self._sm() as session:
            record = await FileRepository(session).get(file_id)
        if record is not None and record.status == FileStatus.DELETING.value:
            await purge_file(self._sm, self._storage, file_id)
        else:
            logger.warning("processing aborted: file no longer PROCESSING", extra={"file_id": file_id})
