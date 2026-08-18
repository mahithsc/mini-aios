"""Execution adapter that projects agent events into durable run events."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Protocol

from aios_core.agent.events import AgentEvent
from aios_core.agent.runtime import AgentRunRequest, AgentRuntime
from aios_core.execution.service import RunsService, build_run_event
from server.types.run import Run, RunEvent


class StreamedAgentRuntime(Protocol):
    def run(self, request: AgentRunRequest) -> AsyncIterator[AgentEvent]: ...


class ActivityIndicator(Protocol):
    async def set_mode(self, mode: str) -> None: ...


class _NoopActivityIndicator:
    async def set_mode(self, mode: str) -> None:
        del mode


def _project_agent_event(
    event: AgentEvent,
    *,
    run_id: str,
    chat_id: str,
) -> RunEvent:
    data: dict[str, object] | None = None

    if event.kind == "text_delta":
        event_type = "token"
        data = {"value": event.value or ""}
    elif event.kind == "tool_call_start":
        event_type = "tool_call_start"
        data = {
            "toolCallId": event.tool_call_id,
            "toolName": event.tool_name or "tool",
            "input": event.input,
        }
    elif event.kind == "tool_call_end":
        event_type = "tool_call_end"
        data = {
            "toolCallId": event.tool_call_id,
            "toolName": event.tool_name or "tool",
            "output": event.output,
        }
    elif event.kind == "subagent_tool_event":
        event_type = "subagent_tool_event"
        data = {
            "parentToolCallId": event.parent_tool_call_id,
            "childRunId": event.child_run_id,
            "childEventType": event.child_event_type,
            "toolCallId": event.tool_call_id,
            "toolName": event.tool_name,
            "input": event.input,
            "output": event.output,
            "error": event.error,
        }
    elif event.kind == "error":
        event_type = "error"
        data = {"error": event.error or "Agent run failed."}
    elif event.kind in {"started", "completed"}:
        event_type = event.kind
    else:  # pragma: no cover - protects callers if the public union expands.
        raise ValueError(f"Unsupported agent event: {event.kind}")

    return build_run_event(
        run_id=run_id,
        event_type=event_type,
        chat_id=chat_id,
        data=data,
    )


class ChatRunner:
    kind = "chat"

    def __init__(
        self,
        runtime: StreamedAgentRuntime | None = None,
        *,
        activity: ActivityIndicator | None = None,
    ) -> None:
        self._runtime = runtime or AgentRuntime()
        self._activity = activity or _NoopActivityIndicator()

    async def execute(self, run: Run, runs_service: RunsService) -> None:
        chat_id = run.chatId
        if not chat_id:
            await runs_service.emit_event(
                run.id,
                build_run_event(
                    run_id=run.id,
                    event_type="error",
                    chat_id=None,
                    data={"error": "Chat run is missing chatId."},
                ),
            )
            return

        events = self._runtime.run(
            AgentRunRequest(
                run_id=run.id,
                chat_id=chat_id,
                turn_id=run.turnId,
            )
        )
        thinking = False
        try:
            async for event in events:
                await runs_service.emit_event(
                    run.id,
                    _project_agent_event(
                        event,
                        run_id=run.id,
                        chat_id=chat_id,
                    ),
                )
                if event.kind == "started":
                    await self._activity.set_mode("thinking")
                    thinking = True
        except Exception as exc:
            # Tell the agent runtime that its consumer failed. A plain close
            # represents task/user cancellation; injecting the exception lets
            # the runtime persist this path as an infrastructure error.
            throw = getattr(events, "athrow", None)
            if throw is not None:
                with suppress(BaseException):
                    await throw(exc)
            raise
        finally:
            close = getattr(events, "aclose", None)
            if close is not None:
                with suppress(BaseException):
                    await close()
            if thinking:
                await self._activity.set_mode("idle")


__all__ = ["ActivityIndicator", "ChatRunner"]
