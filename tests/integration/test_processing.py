"""Reliability behaviour: crash/retry idempotency, cancellation, budgets, sweeper, arq policy."""

from __future__ import annotations

import asyncio
import os
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest
from arq import Retry

from app.core.clock import utcnow
from app.core.exceptions import FileNotFound, InvalidState
from app.core.ids import new_id
from app.models.uploaded_file import UploadedFile
from app.repositories.feature_repo import FeatureRepository
from app.repositories.file_repo import FileRepository
from app.services.processing_service import ProcessingService
from app.workers.tasks import process_file
from tests.helpers import BLR_SQUARE, build_shapefile_zip, build_zip, kml_document, kml_placemark, kml_point


def square(i: int):
    dx = i * 0.002
    return [[(77.59 + dx, 12.97), (77.591 + dx, 12.97), (77.591 + dx, 12.971), (77.59 + dx, 12.971)]]


def shapefile_of(n: int) -> bytes:
    return build_shapefile_zip("polygon", [square(i) for i in range(n)])


# --------------------------------------------------------------------------- happy path


async def test_worker_style_flow_pending_then_completed(make_env):
    env = await make_env()
    record = await env.ingest(shapefile_of(12), "p.zip")
    assert record.status == "PENDING" and env.dispatcher.enqueued == [record.id]

    await env.processing.process(record.id)
    done = await env.get(record.id)
    assert done.status == "COMPLETED" and done.feature_count == 12 and done.processed_features == 12
    assert done.crs == "EPSG:4326" and done.completed_at is not None
    assert done.stats["by_status"] == {"MEASURED": 12}
    assert await env.feature_count(record.id) == 12


async def test_reprocessing_a_completed_file_is_a_noop(make_env):
    env = await make_env()
    record = await env.ingest(shapefile_of(12), "p.zip")
    await env.processing.process(record.id)
    before = await env.get(record.id)
    await env.processing.process(record.id)  # duplicate job delivery
    await env.processing.process(record.id, reclaim=True)  # even a "retry" delivery
    after = await env.get(record.id)
    assert after.status == "COMPLETED" and after.attempts == before.attempts == 1
    assert await env.feature_count(record.id) == 12


# --------------------------------------------------------------------------- crash safety


async def test_crash_mid_file_then_retry_leaves_no_duplicates_or_partials(make_env, monkeypatch):
    env = await make_env()  # READ_BATCH_SIZE=5 -> batches of 5, 5, 2
    record = await env.ingest(shapefile_of(12), "p.zip")

    original = ProcessingService._measure
    calls = {"n": 0}

    def flaky(self, batch, start_index, crs, file_id):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("worker died")
        return original(self, batch, start_index, crs, file_id)

    monkeypatch.setattr(ProcessingService, "_measure", flaky)
    with pytest.raises(RuntimeError, match="worker died"):
        await env.processing.process(record.id)

    crashed = await env.get(record.id)
    assert crashed.status == "PROCESSING"
    assert await env.feature_count(record.id) == 5  # first batch committed, rest missing

    # A client polling now must not see partial data...
    from app.api.routes.files import _completed_file
    from app.core.exceptions import FileNotReady

    async with env.sessionmaker() as session:
        with pytest.raises(FileNotReady):
            await _completed_file(session, record.id)

    # ...and the arq retry (job_try > 1) wipes the partial rows and redoes the whole file.
    monkeypatch.setattr(ProcessingService, "_measure", original)
    await env.processing.process(record.id, reclaim=True)
    done = await env.get(record.id)
    assert done.status == "COMPLETED" and done.attempts == 2
    assert await env.feature_count(record.id) == 12  # exactly once each

    async with env.sessionmaker() as session:
        rows = await FeatureRepository(session).page(record.id, after_index=-1, limit=100)
    assert [r.feature_index for r in rows] == list(range(12))


async def test_a_second_worker_cannot_steal_a_file_that_is_processing(make_env):
    env = await make_env()
    record = await env.ingest(shapefile_of(3), "p.zip")
    async with env.sessionmaker() as session:
        assert await FileRepository(session).claim(record.id, allow_reclaim=False)
        await session.commit()
    await env.processing.process(record.id)  # duplicate delivery, no reclaim -> skipped
    assert (await env.get(record.id)).status == "PROCESSING"
    assert await env.feature_count(record.id) == 0


# --------------------------------------------------------------------------- cancellation


async def test_delete_during_processing_stops_the_job_and_purges_everything(make_env, monkeypatch):
    env = await make_env()
    record = await env.ingest(shapefile_of(12), "p.zip")
    blob_dir = env.storage.files_dir / record.id
    assert blob_dir.exists()

    original = FileRepository.update_progress
    calls = {"n": 0}

    async def delete_before_second_batch(self, file_id, processed):
        calls["n"] += 1
        if calls["n"] == 2:
            await env.lifecycle.request_delete(file_id)  # a client DELETEs mid-run
        return await original(self, file_id, processed)

    monkeypatch.setattr(FileRepository, "update_progress", delete_before_second_batch)
    await env.processing.process(record.id)

    assert calls["n"] == 2  # stopped at the batch boundary, did not run to the end
    assert await env.get(record.id) is None  # worker purged the row itself
    assert await env.feature_count(record.id) == 0
    assert not blob_dir.exists()


async def test_delete_of_idle_file_then_background_purge(make_env):
    env = await make_env()
    record = await env.ingest(shapefile_of(3), "p.zip")
    await env.processing.process(record.id)
    previous = await env.lifecycle.request_delete(record.id)
    assert previous == "COMPLETED"
    assert (await env.get(record.id)).status == "DELETING"
    await env.lifecycle.finish_delete(record.id, previous)
    assert await env.get(record.id) is None and await env.feature_count(record.id) == 0
    with pytest.raises(FileNotFound):
        await env.lifecycle.request_delete(record.id)


async def test_delete_of_processing_file_is_left_to_the_worker(make_env):
    env = await make_env()
    record = await env.ingest(shapefile_of(3), "p.zip")
    async with env.sessionmaker() as session:
        await FileRepository(session).claim(record.id, allow_reclaim=False)
        await session.commit()
    previous = await env.lifecycle.request_delete(record.id)
    await env.lifecycle.finish_delete(record.id, previous)  # must NOT unlink under the worker
    assert (env.storage.files_dir / record.id).exists()
    assert (await env.get(record.id)).status == "DELETING"


# --------------------------------------------------------------------------- budgets & failures


async def test_feature_budget_fails_fast_before_reading(make_env):
    env = await make_env(MAX_FEATURES=5)
    record = await env.ingest(shapefile_of(12), "p.zip")
    await env.processing.process(record.id)
    failed = await env.get(record.id)
    assert failed.status == "FAILED" and "12 features" in failed.error
    assert await env.feature_count(record.id) == 0


async def test_kml_budget_is_enforced_while_streaming_when_count_unknown(make_env):
    env = await make_env(MAX_VERTICES=3)
    kml = kml_document(("F", [kml_placemark(f"p{i}", kml_point(1, 1)) for i in range(8)]))
    record = await env.ingest(kml, "k.kml")
    await env.processing.process(record.id)
    failed = await env.get(record.id)
    assert failed.status == "FAILED" and "vertices" in failed.error
    assert await env.feature_count(record.id) == 0  # partial rows wiped on failure


async def test_corrupt_file_fails_fast_with_actionable_message_and_can_be_retried(make_env):
    env = await make_env()
    bad = build_zip({"a.shp": b"garbage" * 50, "a.shx": b"x" * 100, "a.dbf": b"y" * 100, "a.prj": b"GEOGCS[]"})
    record = await env.ingest(bad, "bad.zip")
    await env.processing.process(record.id)
    failed = await env.get(record.id)
    assert failed.status == "FAILED" and "could not be opened" in failed.error

    retried = await env.lifecycle.request_retry(record.id)
    assert retried.status == "PENDING" and retried.error is None
    assert env.dispatcher.enqueued == [record.id, record.id]

    with pytest.raises(InvalidState):
        await env.lifecycle.request_retry(record.id)  # no longer FAILED


async def test_missing_blob_is_a_permanent_failure(make_env):
    env = await make_env()
    record = await env.ingest(shapefile_of(3), "p.zip")
    await env.storage.delete_file(record.id)
    await env.processing.process(record.id)
    assert (await env.get(record.id)).status == "FAILED"


async def test_empty_layer_completes_with_a_warning(make_env):
    env = await make_env()
    record = await env.ingest(build_shapefile_zip("polygon", []), "empty.zip")
    await env.processing.process(record.id)
    done = await env.get(record.id)
    assert done.status == "COMPLETED" and done.feature_count == 0 and done.warnings == ["NO_FEATURES"]


async def test_features_are_stored_in_the_source_crs_as_wkb(make_env):
    import shapely

    env = await make_env()
    record = await env.ingest(shapefile_of(2), "p.zip")
    await env.processing.process(record.id)
    async with env.sessionmaker() as session:
        row = (await FeatureRepository(session).page(record.id, after_index=-1, limit=1))[0]
    geom = shapely.from_wkb(row.geometry_wkb)
    assert geom.geom_type == "Polygon" and geom.bounds[0] == pytest.approx(77.59)


# --------------------------------------------------------------------------- sweeper


async def insert_file(env, *, status, updated_at=None, started_at=None, blob=True) -> str:
    file_id = new_id()
    if blob:
        directory = env.storage.files_dir / file_id
        directory.mkdir()
        (directory / "original.kml").write_bytes(b"<kml/>")
    now = utcnow()
    async with env.sessionmaker() as session:
        session.add(
            UploadedFile(
                id=file_id, filename="x.kml", source_format="kml", storage_key=f"{file_id}/original.kml",
                size_bytes=6, sha256="0" * 64, status=status, warnings=[], client_id="t",
                created_at=now, updated_at=updated_at or now, started_at=started_at,
            )
        )
        await session.commit()
    return file_id


async def test_sweeper_recovers_stuck_files_and_cleans_storage(make_env):
    env = await make_env(
        STALE_PENDING_S=60, STALE_PROCESSING_S=60, DELETE_GRACE_S=60, FAILED_RETENTION_S=60,
        ORPHAN_BLOB_GRACE_S=60, STALE_TMP_AGE_S=60,
    )
    hour_ago = utcnow() - timedelta(hours=1)
    stale_pending = await insert_file(env, status="PENDING", updated_at=hour_ago)
    stale_processing = await insert_file(env, status="PROCESSING", started_at=hour_ago)
    fresh_processing = await insert_file(env, status="PROCESSING", started_at=utcnow())
    stale_deleting = await insert_file(env, status="DELETING", updated_at=hour_ago)
    fresh_deleting = await insert_file(env, status="DELETING")
    old_failed = await insert_file(env, status="FAILED", updated_at=hour_ago)
    fresh_failed = await insert_file(env, status="FAILED")

    old_orphan, new_orphan = new_id(), new_id()
    for orphan in (old_orphan, new_orphan):
        (env.storage.files_dir / orphan).mkdir()
        (env.storage.files_dir / orphan / "original.kml").write_bytes(b"x")
    long_ago = time.time() - 3600
    os.utime(env.storage.files_dir / old_orphan, (long_ago, long_ago))

    stale_tmp = env.storage.new_temp_path(".zip")
    stale_tmp.write_bytes(b"partial upload")
    os.utime(stale_tmp, (long_ago, long_ago))
    fresh_tmp = env.storage.new_temp_path(".zip")
    fresh_tmp.write_bytes(b"in flight")

    report = await env.lifecycle.run_sweep()
    assert report == {
        "temp_removed": 1, "stale_pending_failed": 1, "stale_processing_failed": 1,
        "purged": 2, "orphan_blobs_removed": 1,
    }

    assert (await env.get(stale_pending)).status == "FAILED"
    assert "never started" in (await env.get(stale_pending)).error
    assert (await env.get(stale_processing)).status == "FAILED"
    assert (await env.get(fresh_processing)).status == "PROCESSING"
    assert await env.get(stale_deleting) is None and not (env.storage.files_dir / stale_deleting).exists()
    assert (await env.get(fresh_deleting)).status == "DELETING"
    assert await env.get(old_failed) is None and not (env.storage.files_dir / old_failed).exists()
    assert (await env.get(fresh_failed)).status == "FAILED"
    assert not (env.storage.files_dir / old_orphan).exists()
    assert (env.storage.files_dir / new_orphan).exists()  # young orphans may be mid-upload
    assert not stale_tmp.exists() and fresh_tmp.exists()

    assert await env.lifecycle.run_sweep() == {
        "temp_removed": 0, "stale_pending_failed": 0, "stale_processing_failed": 0,
        "purged": 0, "orphan_blobs_removed": 0,
    }  # idempotent


# --------------------------------------------------------------------------- arq task policy


class FakeProcessing:
    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.failed: list[tuple[str, str]] = []
        self.reclaim_flags: list[bool] = []

    async def process(self, file_id, *, reclaim=False):
        self.reclaim_flags.append(reclaim)
        await self.behaviour()

    async def mark_failed(self, file_id, error):
        self.failed.append((file_id, error))


def ctx_for(fake, job_try, **overrides):
    settings = SimpleNamespace(JOB_TIMEOUT_S=overrides.pop("timeout", 5), JOB_MAX_TRIES=3, JOB_RETRY_BASE_DELAY_S=5)
    return {"processing": fake, "settings": settings, "job_try": job_try}


async def test_arq_task_retries_transient_errors_with_exponential_backoff():
    async def boom():
        raise ConnectionError("db blip")

    fake = FakeProcessing(boom)
    with pytest.raises(Retry) as first:
        await process_file(ctx_for(fake, 1), "f1")
    with pytest.raises(Retry) as second:
        await process_file(ctx_for(fake, 2), "f1")
    assert first.value.defer_score == 5_000 and second.value.defer_score == 10_000  # 5s, 10s (ms)
    assert fake.reclaim_flags == [False, True]  # retries are allowed to reclaim PROCESSING
    assert fake.failed == []


async def test_arq_task_marks_file_failed_after_the_last_try():
    async def boom():
        raise ConnectionError("db down")

    fake = FakeProcessing(boom)
    await process_file(ctx_for(fake, 3), "f1")  # does not raise: arq must not retry again
    assert len(fake.failed) == 1 and "after 3 attempts" in fake.failed[0][1]


async def test_arq_task_job_deadline_records_a_useful_failure_without_retrying():
    async def slow():
        await asyncio.sleep(5)

    fake = FakeProcessing(slow)
    await process_file(ctx_for(fake, 1, timeout=0.05), "f1")
    assert "time limit" in fake.failed[0][1]


async def test_arq_task_success_path():
    async def fine():
        return None

    fake = FakeProcessing(fine)
    await process_file(ctx_for(fake, 1), "f1")
    assert fake.failed == []


def test_unused_import_guard():
    assert BLR_SQUARE
