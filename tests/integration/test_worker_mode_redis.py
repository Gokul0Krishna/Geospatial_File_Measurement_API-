"""Worker mode against a REAL Redis (skipped unless TEST_REDIS_URL is set).

    TEST_REDIS_URL=redis://localhost:6379/15 pytest tests/integration/test_worker_mode_redis.py

The API runs with the real ``ArqDispatcher``; an in-process arq worker (burst mode)
then drains the queue exactly as ``arq app.workers.settings.WorkerSettings`` would.
"""

import os

import pytest
from arq.connections import RedisSettings
from arq.worker import Worker
from fastapi.testclient import TestClient

from app.main import create_app
from app.services.processing_service import ProcessingService
from app.workers.tasks import process_file
from tests.conftest import upload
from tests.helpers import BLR_SQUARE, build_shapefile_zip

REDIS_URL = os.environ.get("TEST_REDIS_URL")
pytestmark = pytest.mark.skipif(not REDIS_URL, reason="TEST_REDIS_URL not set")


async def drain_queue(app) -> None:
    state = app.state
    worker = Worker(
        functions=[process_file],
        redis_settings=RedisSettings.from_dsn(REDIS_URL),
        ctx={"processing": ProcessingService(state.sessionmaker, state.storage, state.settings), "settings": state.settings},
        burst=True,
        poll_delay=0.05,
        handle_signals=False,
        max_tries=3,
    )
    try:
        await worker.async_run()
    finally:
        await worker.close()


async def test_upload_is_queued_then_processed_by_a_worker(make_settings):
    settings = make_settings(PROCESSING_MODE="worker", REDIS_URL=REDIS_URL)
    with TestClient(create_app(settings)) as client:
        await client.app.state.redis.flushdb()
        response = upload(client, build_shapefile_zip("polygon", [[BLR_SQUARE]]), "p.zip")
        assert response.status_code == 202 and response.json()["status"] == "PENDING"
        file_id = response.json()["id"]
        assert client.get(f"/api/files/{file_id}/measurements/").status_code == 409  # not yet

        assert client.get("/ready").json()["checks"]["redis"] == "ok"
        await drain_queue(client.app)

        info = client.get(f"/api/files/{file_id}/").json()
        assert info["status"] == "COMPLETED" and info["feature_count"] == 1
        items = client.get(f"/api/files/{file_id}/measurements/").json()["items"]
        assert items[0]["measurement"]["value"] > 9e5
