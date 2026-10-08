from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

router = APIRouter(tags=["health"])
_CHECK_TIMEOUT_S = 3.0


@router.get("/health", summary="Liveness: the process is up")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready", summary="Readiness: database, queue and storage are usable")
async def ready(request: Request) -> JSONResponse:
    state = request.app.state
    settings = state.settings

    async def database() -> None:
        async with state.sessionmaker() as session:
            await session.execute(text("SELECT 1"))

    checks: dict[str, Callable[[], Awaitable[object]]] = {
        "database": database,
        "storage": state.storage.check_writable,
    }
    if settings.PROCESSING_MODE == "worker" and state.redis is not None:
        checks["redis"] = state.redis.ping

    results: dict[str, str] = {}
    for name, check in checks.items():
        try:
            await asyncio.wait_for(check(), _CHECK_TIMEOUT_S)
            results[name] = "ok"
        except Exception as exc:
            results[name] = f"error: {exc.__class__.__name__}"

    healthy = all(v == "ok" for v in results.values())
    return JSONResponse(
        {"status": "ready" if healthy else "unavailable", "checks": results},
        status_code=200 if healthy else 503,
    )
