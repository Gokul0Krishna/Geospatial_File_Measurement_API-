from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, File, Path, Query, Request, Response, UploadFile

from app.api.deps import (
    ClientIdDep,
    LifecycleServiceDep,
    SessionDep,
    SettingsDep,
    UploadServiceDep,
)
from app.api.presenters import select_page, to_feature, to_measurement_item
from app.core.exceptions import FileNotFound, FileNotReady
from app.core.pagination import (
    decode_id_cursor,
    decode_index_cursor,
    encode_id_cursor,
    encode_index_cursor,
    resolve_limit,
)
from app.models.feature import Feature
from app.models.uploaded_file import UploadedFile
from app.repositories.feature_repo import FeatureRepository
from app.repositories.file_repo import FileRepository
from app.schemas.enums import FeatureStatus, FileStatus, MeasureMethod
from app.schemas.errors import ProblemDetails
from app.schemas.feature import FeaturePage, MeasurementPage
from app.schemas.file import DeleteOut, FileOut, FilePage, FileSummary

router = APIRouter(prefix="/api/files", tags=["files"])

FileId = Annotated[str, Path(max_length=64, description="File id returned by the upload call.")]
LimitQ = Annotated[int | None, Query(description="Page size (server-side caps apply).")]
CursorQ = Annotated[str | None, Query(max_length=512, description="Opaque `next_cursor` value.")]
MethodQ = Annotated[
    MeasureMethod,
    Query(description="`utm`: planar in the feature's UTM zone. `geodesic`: on the WGS84 ellipsoid."),
]
StatusQ = Annotated[FeatureStatus | None, Query(description="Only features with this status.")]

_NOT_FOUND = {404: {"model": ProblemDetails, "description": "File not found."}}
_NOT_READY = {409: {"model": ProblemDetails, "description": "File is not COMPLETED."}}


async def _completed_file(session: SessionDep, file_id: str) -> UploadedFile:
    record = await FileRepository(session).get_visible(file_id)
    if record is None:
        raise FileNotFound(f"File {file_id} does not exist.")
    if record.status == FileStatus.COMPLETED.value:
        return record
    if record.status == FileStatus.FAILED.value:
        raise FileNotReady(
            f"Processing failed: {record.error or 'unknown error'}",
            extra={"file_status": record.status},
        )
    raise FileNotReady(
        f"The file is still being processed (status: {record.status}). "
        f"Poll GET /api/files/{file_id}/ until it is COMPLETED.",
        headers={"Retry-After": "2"},
        extra={"file_status": record.status},
    )


async def _feature_rows(
    session: SessionDep,
    settings: SettingsDep,
    record: UploadedFile,
    *,
    limit: int | None,
    cursor: str | None,
    status: FeatureStatus | None,
    include_geometry: bool,
) -> tuple[list[Feature], str | None]:
    page_size = resolve_limit(limit, settings, with_geometry=include_geometry)
    after = decode_index_cursor(cursor)
    rows = await FeatureRepository(session).page(
        record.id,
        after_index=after,
        limit=page_size + 1,  # one extra row tells us whether there is a next page
        status=status.value if status else None,
    )
    page, has_more = select_page(
        rows,
        page_size,
        with_geometry=include_geometry,
        max_geometry_bytes=settings.PAGE_MAX_GEOMETRY_BYTES,
    )
    next_cursor = encode_index_cursor(page[-1].feature_index) if has_more and page else None
    return page, next_cursor


@router.post(
    "/",
    response_model=FileOut,
    status_code=201,
    summary="Upload a Shapefile (.zip) or KML file",
    responses={
        202: {"model": FileOut, "description": "Accepted; processing continues in a worker."},
        400: {"model": ProblemDetails, "description": "Malformed or unsafe upload."},
        413: {"model": ProblemDetails, "description": "File too large."},
        415: {"model": ProblemDetails, "description": "Unsupported file type."},
        422: {"model": ProblemDetails, "description": "Valid archive but unusable (e.g. no .prj)."},
        429: {"model": ProblemDetails, "description": "Too many of your files in progress."},
        503: {"model": ProblemDetails, "description": "Service saturated; honour Retry-After."},
    },
)
async def upload_file(
    request: Request,
    response: Response,
    upload_service: UploadServiceDep,
    client_id: ClientIdDep,
    file: Annotated[UploadFile, File(description="`.zip` containing a Shapefile, or a `.kml`.")],
) -> FileOut:
    """Stores the upload and processes it.

    * `PROCESSING_MODE=inline` - processed before responding: **201** with the final
      status (`COMPLETED` or `FAILED`).
    * `PROCESSING_MODE=worker` - queued: **202** with status `PENDING`; poll
      `GET /api/files/{id}/`.
    """
    record = await upload_service.handle_upload(file, client_id)
    response.status_code = 201 if upload_service.dispatcher.is_inline else 202
    response.headers["Location"] = f"/api/files/{record.id}/"
    return FileOut.from_record(record)


@router.get("/", response_model=FilePage, summary="List files (newest first)")
async def list_files(
    session: SessionDep,
    settings: SettingsDep,
    limit: LimitQ = None,
    cursor: CursorQ = None,
    status: Annotated[FileStatus | None, Query(description="Filter by file status.")] = None,
) -> FilePage:
    page_size = resolve_limit(limit, settings)
    rows = await FileRepository(session).list_page(
        limit=page_size + 1, before_id=decode_id_cursor(cursor), status=status
    )
    has_more = len(rows) > page_size
    page = rows[:page_size]
    return FilePage(
        items=[FileOut.from_record(r) for r in page],
        next_cursor=encode_id_cursor(page[-1].id) if has_more and page else None,
    )


@router.get(
    "/{file_id}/",
    response_model=FileOut,
    summary="File information and processing status",
    responses=_NOT_FOUND,
)
async def get_file(file_id: FileId, session: SessionDep) -> FileOut:
    record = await FileRepository(session).get_visible(file_id)
    if record is None:
        raise FileNotFound(f"File {file_id} does not exist.")
    return FileOut.from_record(record)


@router.get(
    "/{file_id}/measurements/",
    response_model=MeasurementPage,
    response_model_exclude_unset=True,
    summary="Measurements for every feature",
    responses={**_NOT_FOUND, **_NOT_READY},
)
async def get_measurements(
    file_id: FileId,
    session: SessionDep,
    settings: SettingsDep,
    method: MethodQ = MeasureMethod.UTM,
    limit: LimitQ = None,
    cursor: CursorQ = None,
    status: StatusQ = None,
    include_geometry: Annotated[bool, Query(description="Also return each GeoJSON geometry.")] = False,
) -> MeasurementPage:
    """Area (m2) for polygons, length (m) for lines. Points, empty and unsupported
    geometries are returned with an explanatory `status` and a null `measurement`."""
    record = await _completed_file(session, file_id)
    rows, next_cursor = await _feature_rows(
        session,
        settings,
        record,
        limit=limit,
        cursor=cursor,
        status=status,
        include_geometry=include_geometry,
    )
    items = [to_measurement_item(r, method, include_geometry=include_geometry) for r in rows]
    return MeasurementPage(
        file_id=record.id,
        method=method,
        crs=record.crs,
        summary=FileSummary.model_validate(record.stats) if record.stats else None,
        count=len(items),
        next_cursor=next_cursor,
        items=items,
    )


@router.get(
    "/{file_id}/features/",
    response_model=FeaturePage,
    response_model_exclude_unset=True,
    summary="Features with geometry, CRS, properties and measurement",
    responses={**_NOT_FOUND, **_NOT_READY},
)
async def get_features(
    file_id: FileId,
    session: SessionDep,
    settings: SettingsDep,
    method: MethodQ = MeasureMethod.UTM,
    limit: LimitQ = None,
    cursor: CursorQ = None,
    status: StatusQ = None,
    include_geometry: Annotated[bool, Query(description="Include GeoJSON geometry.")] = True,
) -> FeaturePage:
    record = await _completed_file(session, file_id)
    rows, next_cursor = await _feature_rows(
        session,
        settings,
        record,
        limit=limit,
        cursor=cursor,
        status=status,
        include_geometry=include_geometry,
    )
    items = [to_feature(r, record.crs, method, include_geometry=include_geometry) for r in rows]
    return FeaturePage(
        file_id=record.id,
        method=method,
        crs=record.crs,
        count=len(items),
        next_cursor=next_cursor,
        items=items,
    )


@router.post(
    "/{file_id}/retry/",
    response_model=FileOut,
    status_code=202,
    summary="Re-run processing of a FAILED file",
    responses={**_NOT_FOUND, 409: {"model": ProblemDetails, "description": "File is not FAILED."}},
)
async def retry_file(file_id: FileId, lifecycle: LifecycleServiceDep) -> FileOut:
    return FileOut.from_record(await lifecycle.request_retry(file_id))


@router.delete(
    "/{file_id}/",
    response_model=DeleteOut,
    status_code=202,
    summary="Delete a file and its features (asynchronous)",
    responses=_NOT_FOUND,
)
async def delete_file(
    file_id: FileId, background: BackgroundTasks, lifecycle: LifecycleServiceDep
) -> DeleteOut:
    """Marks the file `DELETING` (it disappears from the API immediately); data is
    purged in the background, or by the worker that is still processing it."""
    previous = await lifecycle.request_delete(file_id)
    background.add_task(lifecycle.finish_delete, file_id, previous)
    return DeleteOut(id=file_id, status=FileStatus.DELETING)
