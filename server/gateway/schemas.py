from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


class SessionCreate(BaseModel):
    title: str | None = None
    cwd: str | None = None
    model: str | None = None
    provider: str | None = None


class SessionOut(BaseModel):
    id: str
    hermes_session_id: str
    title: str | None = None
    status: str
    created_at: str
    updated_at: str


class MessageCreate(BaseModel):
    content: str = Field(min_length=1)


class MessageOut(BaseModel):
    id: int
    session_id: str
    role: str
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class EventOut(BaseModel):
    id: int
    session_id: str
    hermes_session_id: str | None = None
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class MessageSubmitOut(BaseModel):
    status: str
    session_id: str
    hermes: dict[str, Any] | None = None


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def ms_to_iso_z(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")
