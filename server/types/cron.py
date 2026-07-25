from __future__ import annotations

from pydantic import BaseModel, Field

from .chat import UnixMs


class CronUpcomingItem(BaseModel):
    id: str
    name: str
    description: str
    schedule: str | None = None
    scheduleTimezone: str | None = None
    runAtUtc: str | None = None
    nextRunAt: UnixMs
    lastRunAt: str | None = None
    status: str


class CronUpcomingListResponse(BaseModel):
    crons: list[CronUpcomingItem] = Field(default_factory=list)
