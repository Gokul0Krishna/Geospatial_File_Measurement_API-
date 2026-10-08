from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.services.lifecycle_service import LifecycleService
from app.services.upload_service import UploadService


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessionmaker() as session:
        yield session


def get_upload_service(request: Request) -> UploadService:
    return request.app.state.upload_service  # type: ignore[no-any-return]


def get_lifecycle_service(request: Request) -> LifecycleService:
    return request.app.state.lifecycle_service  # type: ignore[no-any-return]


def get_client_id(request: Request) -> str:
    """Identity used for per-client limits. Behind a proxy run uvicorn with
    --proxy-headers so this is the real client address, not the proxy's."""
    return request.client.host if request.client else "unknown"


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
UploadServiceDep = Annotated[UploadService, Depends(get_upload_service)]
LifecycleServiceDep = Annotated[LifecycleService, Depends(get_lifecycle_service)]
ClientIdDep = Annotated[str, Depends(get_client_id)]
