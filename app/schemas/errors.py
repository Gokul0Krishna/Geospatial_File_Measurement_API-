from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class ProblemDetails(BaseModel):
    """RFC 7807 problem document (served as application/problem+json)."""

    type: str
    title: str
    status: int
    detail: str
    instance: str
    request_id: str
    errors: list[dict[str, Any]] | None = None
