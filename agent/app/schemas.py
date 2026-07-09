from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class DiagnoseRequest(BaseModel):
    message: str = Field(min_length=1)
    cluster: str | None = None
    namespace: str | None = None


class DiagnoseResponse(BaseModel):
    answer: str
    evidence: list[dict[str, Any]]


class RemediateRequest(BaseModel):
    action_type: str = Field(min_length=1)
    namespace: str = Field(min_length=1)
    deployment: str = Field(min_length=1)
    container: str | None = None
    image: str | None = None
    reason: str | None = None


class RemediateResponse(BaseModel):
    status: str
    message: str
    evidence: list[dict[str, Any]]
