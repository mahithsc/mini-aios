"""Stable events emitted by the AIOS agent runtime."""

from __future__ import annotations

import ast
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
    """Decode the two UI tools that still return serialized objects."""
    if tool_name not in {"show_canvas", "generative_widget"} or not isinstance(
        output, str
    ):
        return output

    try:
        return json.loads(output)
    except json.JSONDecodeError:
        pass

    try:
        return ast.literal_eval(output)
    except (SyntaxError, ValueError):
        return output


__all__ = ["AgentEvent", "AgentEventKind", "normalize_tool_output"]
