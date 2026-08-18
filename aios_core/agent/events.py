"""Stable events emitted by the AIOS agent runtime."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

AgentEventKind = Literal[
    "started",
    "text_delta",
    "tool_call_start",
    "tool_call_end",
    "subagent_tool_event",
    "completed",
    "error",
]


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """Provider-neutral event produced by one agent invocation."""

    kind: AgentEventKind
    value: str | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    input: object | None = None
    output: object | None = None
    error: str | None = None
    parent_tool_call_id: str | None = None
    child_run_id: str | None = None
    child_event_type: str | None = None

__all__ = ["AgentEvent", "AgentEventKind"]
