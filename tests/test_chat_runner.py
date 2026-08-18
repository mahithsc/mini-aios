from __future__ import annotations

import asyncio
from types import SimpleNamespace

from aios_core.agent.events import AgentEvent
from aios_core.execution.runners import chat
from server.types.run import Run


class _Runtime:
    def __init__(self, events: list[AgentEvent]) -> None:
        self.events = events
        self.requests = []
        self.closed = False
        self.consumer_error: Exception | None = None

    async def run(self, request):
        self.requests.append(request)
        try:
            for event in self.events:
                yield event
        except Exception as exc:
            self.consumer_error = exc
            raise
        finally:
            self.closed = True


class _RunsService:
    def __init__(
        self,
        *,
        cancel_on_token: bool = False,
        fail_on_token: bool = False,
    ) -> None:
        self.events = []
        self.cancel_on_token = cancel_on_token
        self.fail_on_token = fail_on_token

    async def emit_event(self, _run_id, event):
        self.events.append(event)
        if self.cancel_on_token and event.event.type == "token":
            raise asyncio.CancelledError
        if self.fail_on_token and event.event.type == "token":
            raise RuntimeError("projection failed")
        return event


def _run(*, chat_id: str | None = "chat-1") -> Run:
    return Run(
        id="run-1",
        kind="chat",
        status="running",
        createdAt=1,
        updatedAt=1,
        chatId=chat_id,
        turnId="user-current",
    )


def _activity() -> tuple[SimpleNamespace, list[str]]:
    modes: list[str] = []

    async def set_mode(mode: str) -> None:
        modes.append(mode)

    return SimpleNamespace(set_mode=set_mode), modes


def test_chat_runner_only_projects_agent_events() -> None:
    agent_runtime = _Runtime(
        [
            AgentEvent(kind="started"),
            AgentEvent(kind="text_delta", value="Hello"),
            AgentEvent(
                kind="tool_call_start",
                tool_call_id="call-1",
                tool_name="example_tool",
                input={"title": "Demo"},
            ),
            AgentEvent(
                kind="tool_call_end",
                tool_call_id="call-1",
                tool_name="example_tool",
                output={"url": "/demo"},
            ),
            AgentEvent(
                kind="subagent_tool_event",
                parent_tool_call_id="parent-1",
                child_run_id="child-1",
                child_event_type="stream_start",
            ),
            AgentEvent(kind="completed"),
        ]
    )
    service = _RunsService()
    activity, light_modes = _activity()

    asyncio.run(
        chat.ChatRunner(agent_runtime, activity=activity).execute(_run(), service)
    )

    assert [event.event.type for event in service.events] == [
        "started",
        "token",
        "tool_call_start",
        "tool_call_end",
        "subagent_tool_event",
        "completed",
    ]
    assert service.events[1].event.data == {"value": "Hello"}
    assert service.events[3].event.data == {
        "toolCallId": "call-1",
        "toolName": "example_tool",
        "output": {"url": "/demo"},
    }
    assert service.events[4].event.data == {
        "parentToolCallId": "parent-1",
        "childRunId": "child-1",
        "childEventType": "stream_start",
        "toolCallId": None,
        "toolName": None,
        "input": None,
        "output": None,
        "error": None,
    }
    [request] = agent_runtime.requests
    assert (request.run_id, request.chat_id, request.turn_id) == (
        "run-1",
        "chat-1",
        "user-current",
    )
    assert agent_runtime.closed is True
    assert light_modes == ["thinking", "idle"]


def test_chat_runner_closes_agent_stream_when_projection_is_cancelled() -> None:
    agent_runtime = _Runtime(
        [
            AgentEvent(kind="started"),
            AgentEvent(kind="text_delta", value="partial"),
            AgentEvent(kind="completed"),
        ]
    )
    service = _RunsService(cancel_on_token=True)
    activity, light_modes = _activity()

    async def execute() -> None:
        await chat.ChatRunner(agent_runtime, activity=activity).execute(_run(), service)

    try:
        asyncio.run(execute())
    except asyncio.CancelledError:
        pass
    else:  # pragma: no cover - guards cancellation semantics.
        raise AssertionError("expected chat projection cancellation")

    assert agent_runtime.closed is True
    assert [event.event.type for event in service.events] == ["started", "token"]
    assert light_modes == ["thinking", "idle"]


def test_chat_runner_reports_projection_failure_to_agent_runtime() -> None:
    agent_runtime = _Runtime(
        [
            AgentEvent(kind="started"),
            AgentEvent(kind="text_delta", value="partial"),
        ]
    )
    service = _RunsService(fail_on_token=True)
    activity, light_modes = _activity()

    async def execute() -> None:
        await chat.ChatRunner(agent_runtime, activity=activity).execute(_run(), service)

    try:
        asyncio.run(execute())
    except RuntimeError as exc:
        assert str(exc) == "projection failed"
    else:  # pragma: no cover - guards failure propagation.
        raise AssertionError("expected projection failure")

    assert isinstance(agent_runtime.consumer_error, RuntimeError)
    assert agent_runtime.closed is True
    assert light_modes == ["thinking", "idle"]


def test_chat_runner_rejects_missing_chat_id() -> None:
    agent_runtime = _Runtime([])
    service = _RunsService()
    activity, light_modes = _activity()

    asyncio.run(
        chat.ChatRunner(agent_runtime, activity=activity).execute(
            _run(chat_id=None), service
        )
    )

    assert [event.event.type for event in service.events] == ["error"]
    assert service.events[0].event.data == {"error": "Chat run is missing chatId."}
    assert agent_runtime.requests == []
    assert light_modes == []
