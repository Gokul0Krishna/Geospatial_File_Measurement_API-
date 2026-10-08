from __future__ import annotations

from typing import Any

from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.feature import Feature

# Keep each INSERT well below MySQL's default max_allowed_packet (16-64 MB) and
# Postgres' bind-parameter limit.
_MAX_ROWS_PER_INSERT = 1000
_MAX_BYTES_PER_INSERT = 4 * 1024 * 1024


def _chunk(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    size = 0
    for row in rows:
        row_size = len(row.get("geometry_wkb") or b"") + 512
        if current and (len(current) >= _MAX_ROWS_PER_INSERT or size + row_size > _MAX_BYTES_PER_INSERT):
            chunks.append(current)
            current, size = [], 0
        current.append(row)
        size += row_size
    if current:
        chunks.append(current)
    return chunks


class FeatureRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def bulk_insert(self, rows: list[dict[str, Any]]) -> None:
        for chunk in _chunk(rows):
            await self.session.execute(insert(Feature), chunk)

    async def delete_for_file(self, file_id: str) -> int:
        result = await self.session.execute(delete(Feature).where(Feature.file_id == file_id))
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

    async def page(
        self,
        file_id: str,
        *,
        after_index: int,
        limit: int,
        status: str | None = None,
    ) -> list[Feature]:
        """Keyset page ordered by feature_index (cost is independent of page depth)."""
        stmt = select(Feature).where(Feature.file_id == file_id, Feature.feature_index > after_index)
        if status is not None:
            stmt = stmt.where(Feature.status == status)
        stmt = stmt.order_by(Feature.feature_index).limit(limit)
        return list((await self.session.execute(stmt)).scalars())
