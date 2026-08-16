"""Shared streaming-event protocol for subagent-style tools.

A subagent-style tool (the in-process ``subagent`` worker, or an external
delegate like Codex) blocks until its work finishes, but streams the child's
tool activity to the parent harness so the UI can render nested progress live.
Both kinds emit the same ``SubagentStreamEvent`` shape, keyed by the parent
tool call, so the frontend consumer treats them identically.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SubagentStreamEvent:
    parent_tool_call_id: str
    child_run_id: str
    child_event_type: str
    tool_call_id: str | None = None
    tool_name: str | None = None
    input: object | None = None
    output: object | None = None
    error: str | None = None
    event: str = field(default="CustomEvent", init=False)
    kind: str = field(default="subagent_tool_event", init=False)

    def __str__(self) -> str:
        # Keep nested UI events out of the parent tool's textual result.
        return ""


def build_subagent_stream_event(
    *,
    parent_tool_call_id: str,
    child_run_id: str,
    child_event_type: str,
    tool_call_id: str | None = None,
    tool_name: str | None = None,
    input: object | None = None,
    output: object | None = None,
    error: str | None = None,
) -> SubagentStreamEvent:
    return SubagentStreamEvent(
        parent_tool_call_id=parent_tool_call_id,
        child_run_id=child_run_id,
        child_event_type=child_event_type,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        input=input,
        output=output,
        error=error,
    )
