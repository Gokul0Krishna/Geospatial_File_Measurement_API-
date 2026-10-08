"""Physical removal of a file: features, stored blob, then the file row (last, so a
crash part-way leaves a DELETING row that the sweeper will finish)."""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.repositories.feature_repo import FeatureRepository
from app.repositories.file_repo import FileRepository
from app.storage.base import Storage

logger = logging.getLogger(__name__)


async def purge_file(
    sessionmaker: async_sessionmaker[AsyncSession], storage: Storage, file_id: str
) -> None:
    async with sessionmaker() as session:
        await FeatureRepository(session).delete_for_file(file_id)
        await session.commit()
    await storage.delete_file(file_id)
    async with sessionmaker() as session:
        await FileRepository(session).delete_row(file_id)
        await session.commit()
    logger.info("file purged", extra={"file_id": file_id})
