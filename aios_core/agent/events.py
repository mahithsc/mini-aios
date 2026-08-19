"""Stable events emitted by the AIOS agent runtime."""

from __future__ import annotations

import json
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


def normalize_tool_output(tool_name: str | None, output: object) -> object:
    """Keep artifact tool completions structured across SDK serialization."""

    if tool_name != "artifact" or not isinstance(output, str):
        return output
    try:
        parsed = json.loads(output)
    except json.JSONDecodeError:
        return output
    return parsed if isinstance(parsed, dict) else output


__all__ = ["AgentEvent", "AgentEventKind", "normalize_tool_output"]
