"""Accepting an upload: backpressure, bounded streaming, validation, storage, enqueue."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from pathlib import Path

import anyio
from fastapi import UploadFile
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.exceptions import (
    InvalidUpload,
    PayloadTooLarge,
    ServiceBusy,
    TooManyActiveFiles,
    UnprocessableUpload,
    UnsupportedMediaType,
)
from app.core.ids import new_id
from app.models.uploaded_file import UploadedFile
from app.repositories.file_repo import FileRepository
from app.schemas.enums import FileStatus, SourceFormat
from app.services.purge import purge_file
from app.storage.base import Storage
from app.utils.zip_safety import (
    MissingProjectionError,
    ZipLimits,
    ZipValidationError,
    inspect_shapefile_zip,
)
from app.workers.dispatcher import JobDispatcher

logger = logging.getLogger(__name__)

_SNIFF_BYTES = 64 * 1024
_ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

_EXTENSIONS: dict[str, SourceFormat] = {".zip": SourceFormat.SHAPEFILE, ".kml": SourceFormat.KML}


def sanitize_filename(raw: str | None) -> str:
    """Display-only name. It is never used to build a path."""
    name = (raw or "upload").replace("\\", "/").rsplit("/", 1)[-1]
    name = _CONTROL_CHARS.sub("", name).strip() or "upload"
    return name[:255]


class UploadService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        storage: Storage,
        settings: Settings,
        dispatcher: JobDispatcher,
    ) -> None:
        self._sm = sessionmaker
        self._storage = storage
        self._settings = settings
        self.dispatcher = dispatcher
        self._zip_limits = ZipLimits(
            max_entries=settings.MAX_ZIP_ENTRIES,
            max_uncompressed_bytes=settings.MAX_ZIP_UNCOMPRESSED_BYTES,
            max_ratio=settings.MAX_ZIP_COMPRESSION_RATIO,
            ratio_min_bytes=settings.ZIP_RATIO_MIN_BYTES,
        )

    async def handle_upload(self, upload: UploadFile, client_id: str) -> UploadedFile:
        filename = sanitize_filename(upload.filename)
        extension = Path(filename).suffix.lower()
        source_format = _EXTENSIONS.get(extension)
        if source_format is None:
            raise UnsupportedMediaType("Upload a .zip containing a Shapefile, or a .kml file.")

        await self._check_capacity(client_id)

        limit = (
            self._settings.MAX_KML_BYTES
            if source_format is SourceFormat.KML
            else self._settings.MAX_UPLOAD_BYTES
        )
        temp_path = self._storage.new_temp_path(extension)
        file_id = new_id()
        key = f"{file_id}/original{extension}"
        try:
            size, digest = await self._stream_to_temp(upload, temp_path, limit)
            await self._validate(source_format, temp_path)
            await self._storage.commit(temp_path, key)
        finally:
            temp_path.unlink(missing_ok=True)  # no-op once committed

        record = UploadedFile(
            id=file_id,
            filename=filename,
            source_format=source_format.value,
            storage_key=key,
            size_bytes=size,
            sha256=digest,
            status=FileStatus.PENDING.value,
            client_id=client_id[:64],
            warnings=[],
            attempts=0,
            processed_features=0,
        )
        try:
            async with self._sm() as session:
                await FileRepository(session).add(record)
                await session.commit()
        except Exception:
            await self._storage.delete_file(file_id)  # don't leak the blob
            raise

        try:
            await self.dispatcher.enqueue(file_id)
        except Exception as exc:
            logger.exception("enqueue failed", extra={"file_id": file_id})
            await purge_file(self._sm, self._storage, file_id)
            raise ServiceBusy(
                "The file could not be queued for processing; please retry shortly.",
                headers={"Retry-After": "10"},
            ) from exc

        async with self._sm() as session:  # inline mode: this is the final state
            fresh = await FileRepository(session).get(file_id)
        return fresh or record

    # ------------------------------------------------------------------ steps

    async def _check_capacity(self, client_id: str) -> None:
        async with self._sm() as session:
            repo = FileRepository(session)
            if await repo.count_active() >= self._settings.MAX_INFLIGHT_JOBS:
                raise ServiceBusy(
                    "The service is processing the maximum number of files; retry shortly.",
                    headers={"Retry-After": "30"},
                )
            if await repo.count_active(client_id) >= self._settings.MAX_ACTIVE_FILES_PER_CLIENT:
                raise TooManyActiveFiles(
                    "You already have the maximum number of files in progress; wait for one "
                    "to finish before uploading another.",
                    headers={"Retry-After": "10"},
                )

    async def _stream_to_temp(self, upload: UploadFile, temp_path: Path, limit: int) -> tuple[int, str]:
        """Copy the upload to staging in chunks, enforcing the cap on bytes actually read
        (never trusting Content-Length) and hashing on the way."""
        hasher = hashlib.sha256()
        size = 0
        async with await anyio.open_file(temp_path, "wb") as out:
            while chunk := await upload.read(self._settings.UPLOAD_CHUNK_BYTES):
                size += len(chunk)
                if size > limit:
                    raise PayloadTooLarge(f"The file exceeds the {limit} byte limit for this type.")
                hasher.update(chunk)
                await out.write(chunk)
        if size == 0:
            raise InvalidUpload("The uploaded file is empty.")
        return size, hasher.hexdigest()

    async def _validate(self, source_format: SourceFormat, path: Path) -> None:
        head = await asyncio.to_thread(_read_head, path)
        if source_format is SourceFormat.SHAPEFILE:
            if not head.startswith(_ZIP_MAGIC):
                raise InvalidUpload("The file is not a zip archive.")
            try:
                await asyncio.to_thread(
                    inspect_shapefile_zip,
                    path,
                    self._zip_limits,
                    require_prj=self._settings.REQUIRE_PRJ,
                )
            except MissingProjectionError as exc:
                raise UnprocessableUpload(str(exc)) from exc
            except ZipValidationError as exc:
                raise InvalidUpload(str(exc)) from exc
            return

        lowered = head.lower()
        if b"<kml" not in lowered:
            raise InvalidUpload("The file does not look like KML (no <kml> element found).")
        if b"<!entity" in lowered:
            # Defence in depth against entity-expansion ("billion laughs") payloads.
            raise InvalidUpload("KML files with DTD entity declarations are not accepted.")


def _read_head(path: Path) -> bytes:
    with open(path, "rb") as handle:
        return handle.read(_SNIFF_BYTES)
