"""Pure-ASGI middleware (BaseHTTPMiddleware would buffer streaming bodies).

* Request correlation: honours an incoming ``X-Request-ID`` (sanitised) or mints one,
  puts it in the logging context and echoes it on the response.
* Body-size cap: Starlette spools a multipart upload to disk *before* the route handler
  runs, so a handler-level check cannot stop a huge or chunked (no Content-Length)
  upload from filling the disk.  We count bytes as they arrive from the socket and
  abort with 413 the moment the cap is crossed.
"""

from __future__ import annotations

import re
import uuid

from fastapi.exceptions import HTTPException
from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.exceptions import problem_response
from app.core.logging import request_id_var

_BODY_METHODS = {"POST", "PUT", "PATCH"}
_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]")


def _request_id(raw: str | None) -> str:
    cleaned = _SAFE_ID.sub("", raw or "")[:64]
    return cleaned or uuid.uuid4().hex


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp, *, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        request_id = _request_id(headers.get("x-request-id"))
        token = request_id_var.set(request_id)

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        try:
            if scope["method"] in _BODY_METHODS:
                declared = headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > self.max_body_bytes:
                    response = problem_response(
                        Request(scope),
                        status=413,
                        title="Payload Too Large",
                        detail=f"The request body exceeds {self.max_body_bytes} bytes.",
                        slug="payload-too-large",
                    )
                    await response(scope, receive, send_with_id)
                    return
                receive = self._counting(receive)
            await self.app(scope, receive, send_with_id)
        finally:
            request_id_var.reset(token)

    def _counting(self, receive: Receive) -> Receive:
        received = 0
        limit = self.max_body_bytes

        async def counting_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    # FastAPI re-raises HTTPException from body parsing untouched
                    # (any other exception would be turned into a generic 400).
                    raise HTTPException(
                        status_code=413, detail=f"The request body exceeds {limit} bytes."
                    )
            return message

        return counting_receive
