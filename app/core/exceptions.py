"""Domain errors and their RFC 7807 (problem+json) rendering."""

from __future__ import annotations

import logging
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import request_id_var

logger = logging.getLogger(__name__)

PROBLEM_JSON = "application/problem+json"
_HTTP_SLUGS = {404: "not-found", 405: "method-not-allowed", 413: "payload-too-large"}


class AppError(Exception):
    """Base class: every subclass maps to one HTTP status and problem type."""

    status_code = 500
    title = "Internal server error"
    slug = "internal-error"

    def __init__(
        self,
        detail: str,
        *,
        headers: dict[str, str] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.headers = headers or {}
        self.extra = extra or {}


class InvalidUpload(AppError):
    status_code = 400
    title = "Invalid upload"
    slug = "invalid-upload"


class UnsupportedMediaType(AppError):
    status_code = 415
    title = "Unsupported file type"
    slug = "unsupported-media-type"


class PayloadTooLarge(AppError):
    status_code = 413
    title = "Payload too large"
    slug = "payload-too-large"


class UnprocessableUpload(AppError):
    status_code = 422
    title = "Unprocessable upload"
    slug = "unprocessable-upload"


class InvalidParameter(AppError):
    status_code = 422
    title = "Invalid parameter"
    slug = "invalid-parameter"


class InvalidCursor(AppError):
    status_code = 400
    title = "Invalid pagination cursor"
    slug = "invalid-cursor"


class FileNotFound(AppError):
    status_code = 404
    title = "File not found"
    slug = "file-not-found"


class FileNotReady(AppError):
    status_code = 409
    title = "File is not ready"
    slug = "file-not-ready"


class InvalidState(AppError):
    status_code = 409
    title = "Operation not allowed in the current state"
    slug = "invalid-state"


class TooManyActiveFiles(AppError):
    status_code = 429
    title = "Too many files in progress"
    slug = "too-many-active-files"


class ServiceBusy(AppError):
    status_code = 503
    title = "Service busy"
    slug = "service-busy"


def problem_response(
    request: Request,
    *,
    status: int,
    title: str,
    detail: str,
    slug: str,
    headers: dict[str, str] | None = None,
    extra: dict[str, Any] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": f"urn:geo-measure:problem:{slug}",
        "title": title,
        "status": status,
        "detail": detail,
        "instance": request.url.path,
        "request_id": request_id_var.get(),
    }
    for key, value in (extra or {}).items():
        if key not in body:  # extension members must never overwrite the standard ones
            body[key] = value
    return JSONResponse(body, status_code=status, media_type=PROBLEM_JSON, headers=headers)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        return problem_response(
            request,
            status=exc.status_code,
            title=exc.title,
            detail=exc.detail,
            slug=exc.slug,
            headers=exc.headers,
            extra=exc.extra,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {"loc": list(e.get("loc", [])), "msg": e.get("msg", ""), "type": e.get("type", "")}
            for e in exc.errors()
        ]
        return problem_response(
            request,
            status=422,
            title="Request validation failed",
            detail="One or more request parameters are invalid.",
            slug="validation-error",
            extra={"errors": errors},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        try:
            title = HTTPStatus(exc.status_code).phrase
        except ValueError:
            title = "HTTP error"
        return problem_response(
            request,
            status=exc.status_code,
            title=title,
            detail=str(exc.detail),
            slug=_HTTP_SLUGS.get(exc.status_code, f"http-{exc.status_code}"),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error", extra={"path": request.url.path})
        return problem_response(
            request,
            status=500,
            title="Internal server error",
            detail="An unexpected error occurred. Quote the request_id when reporting it.",
            slug="internal-error",
        )
