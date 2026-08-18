"""The single model-visible tool for controlling Pi coding-agent jobs."""

from __future__ import annotations

from typing import Any, Literal

from ..agent.context import get_current_chat_id
from .pi_job import PiProfile, get_pi_job_manager

PiAction = Literal["start", "poll", "steer", "stop", "list"]


def pi(
    action: PiAction,
    task: str | None = None,
    job_id: str | None = None,
    path: str = ".",
    message: str | None = None,
    cursor: int = 0,
    wait: float = 0.0,
    model: str | None = None,
    provider: str | None = None,
    thinking_level: str | None = None,
    profile: PiProfile = "coding",
    fc=None,
) -> dict[str, Any]:
    """Start, inspect, steer, or stop a managed Pi coding-agent job.

    Actions:
      - ``start`` requires ``task`` and returns a ``job_id``. ``path`` must be
        inside the current chat files, workspace, or an administrator-configured
        Pi root. This selects Pi's starting directory; it is not an OS sandbox.
        Use ``profile='read_only'`` for review/research without write tools.
      - ``poll`` requires ``job_id`` and returns new events after the absolute
        ``cursor`` plus status/result. ``wait`` may long-poll for up to 30 seconds.
      - ``steer`` requires a running ``job_id`` and ``message``. Acceptance means
        Pi queued the steering instruction; delivery occurs inside Pi's turn.
      - ``stop`` requires ``job_id`` and aborts the whole Pi process group.
      - ``list`` returns jobs owned by the current chat.

    Pi cannot see this conversation, so every start task should be complete and
    self-contained, including relevant paths, constraints, and acceptance tests.
    """
    if not isinstance(action, str):
        return {"error": "action is required; use start, poll, steer, stop, or list"}
    normalized_action = action.strip().lower()
    manager = get_pi_job_manager()
    session_id = get_current_chat_id()

    if normalized_action == "start":
        parent_tool_call_id = getattr(fc, "call_id", None)
        return manager.start(
            task or "",
            path=path,
            model=model,
            provider=provider,
            thinking_level=thinking_level,
            profile=profile,
            session_id=session_id,
            parent_tool_call_id=str(parent_tool_call_id) if parent_tool_call_id else None,
        )
    if normalized_action == "poll":
        return manager.poll(job_id or "", cursor=cursor, wait=wait, session_id=session_id)
    if normalized_action == "steer":
        return manager.steer(job_id or "", message or "", session_id=session_id)
    if normalized_action == "stop":
        return manager.stop(job_id or "", session_id=session_id)
    if normalized_action == "list":
        return manager.list(session_id=session_id)
    return {"error": "unknown action; use start, poll, steer, stop, or list"}
