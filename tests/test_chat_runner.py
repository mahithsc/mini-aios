from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from aios_core.execution.runners import chat
from aios_core.tools.subagent_events import build_subagent_stream_event
from server.types.chat import UserMessage
from server.types.run import Run


class _EventStream:
    def __init__(self, events, *, context=None) -> None:
        self._events = iter(events)
        self._context = context
        self._sent_nested = False
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._context is not None and not self._sent_nested:
            self._sent_nested = True
            await self._context.emit(
                build_subagent_stream_event(
                    parent_tool_call_id="parent-1",
                    child_run_id="child-1",
                    child_event_type="stream_start",
                )
            )
        try:
            return next(self._events)
        except StopIteration as exc:
            raise StopAsyncIteration from exc

    async def aclose(self) -> None:
        self.closed = True


class _StreamingResult:
    def __init__(self, events, *, context=None, final_output=None) -> None:
        self.events = _EventStream(events, context=context)
        self.is_complete = False
        self.run_loop_exception = None
        self.final_output = final_output
        self.cancel_modes: list[str] = []

    def stream_events(self):
        return self.events

    def cancel(self, mode="immediate") -> None:
        self.cancel_modes.append(mode)
        self.is_complete = True


class _RunsService:
    def __init__(self, *, cancel_on_token: bool = False) -> None:
        self.events = []
        self.cancel_on_token = cancel_on_token

    async def emit_event(self, _run_id, event):
        self.events.append(event)
        if self.cancel_on_token and event.event.type == "token":
            raise asyncio.CancelledError
        return event


def _run() -> Run:
    return Run(
        id="run-1",
        kind="chat",
        status="running",
        createdAt=1,
        updatedAt=1,
        chatId="chat-1",
        turnId="user-current",
    )


def _patch_runner_dependencies(monkeypatch, sdk_events, *, final_output=None):
    previous_user = UserMessage(
        id="user-previous",
        createdAt=1,
        updatedAt=1,
        status="complete",
        content="Earlier question",
    )
    current_user = UserMessage(
        id="user-current",
        createdAt=2,
        updatedAt=2,
        status="complete",
        content="hello",
    )
    future_user = UserMessage(
        id="user-future",
        createdAt=3,
        updatedAt=3,
        status="complete",
        content="queued after this turn",
    )
    monkeypatch.setattr(
        chat,
        "load_chat_session",
        lambda _chat_id: [previous_user, current_user, future_user],
    )
    monkeypatch.setattr(chat, "create_agent", lambda **_kwargs: object())
    monkeypatch.setattr(
        chat,
        "push_chat_runtime_context",
        lambda _chat_id: (object(), object(), object()),
    )
    popped = []
    monkeypatch.setattr(chat, "pop_chat_runtime_context", popped.append)

    light_modes = []

    async def set_mode(mode):
        light_modes.append(mode)

    monkeypatch.setattr(chat, "lights", SimpleNamespace(set_mode=set_mode))
    captured: dict[str, Any] = {"persistence_order": []}

    class Store:
        def __init__(self) -> None:
            self.calls: list[tuple[Any, ...]] = []

        def create_turn(self, **kwargs) -> None:
            self.calls.append(("create_turn", kwargs))

        def ensure_seeded(self, chat_id, seed_items) -> None:
            self.calls.append(("ensure_seeded", chat_id, seed_items))

        def append_items(self, **kwargs) -> None:
            self.calls.append(("append_items", kwargs))

        def set_turn_status(self, turn_id, status) -> None:
            self.calls.append(("set_turn_status", turn_id, status))

    store = Store()
    monkeypatch.setattr(chat, "ConversationStore", lambda: store)

    class Recorder:
        def __init__(self) -> None:
            self.application_events: list[tuple[str, dict[str, Any]]] = []
            self.sdk_events: list[Any] = []
            self.custom_events: list[Any] = []
            self.finalize_count = 0

        async def record_application_event(self, event_type, payload) -> int:
            self.application_events.append((event_type, dict(payload)))
            return len(self.application_events)

        async def record_sdk_event(self, event) -> int:
            captured["persistence_order"].append(("persist", event))
            self.sdk_events.append(event)
            return len(self.sdk_events)

        async def record_custom_event(self, event) -> int:
            self.custom_events.append(event)
            return len(self.custom_events)

        async def finalize_unfinished_tools(self) -> None:
            self.finalize_count += 1

        async def finish_turn(self, status, payload) -> int:
            self.finalize_count += 1
            event_type = {
                "complete": "run.completed",
                "cancelled": "run.cancelled",
                "error": "run.error",
            }[status]
            self.application_events.append((event_type, dict(payload)))
            return len(self.application_events)

    recorder = Recorder()

    def make_recorder(**kwargs):
        captured["recorder_kwargs"] = kwargs
        return recorder

    monkeypatch.setattr(chat, "ConversationRecorder", make_recorder)

    class Session:
        def __init__(self) -> None:
            self.added_items: list[list[dict[str, Any]]] = []

        async def add_items(self, items) -> None:
            self.added_items.append(items)

    session = Session()

    def make_session(**kwargs):
        captured["session_kwargs"] = kwargs
        return session

    monkeypatch.setattr(chat, "CanonicalConversationSession", make_session)
    hooks = object()
    monkeypatch.setattr(chat, "DurableRunHooks", lambda: hooks)

    def translate(event):
        captured["persistence_order"].append(("translate", event))
        return event

    monkeypatch.setattr(
        chat,
        "OpenAIEventTranslator",
        lambda: SimpleNamespace(translate=translate),
    )

    def run_streamed(agent, input, **kwargs):
        captured.update(agent=agent, input=input, **kwargs)
        result = _StreamingResult(
            sdk_events,
            context=kwargs["context"],
            final_output=final_output,
        )
        captured["result"] = result
        return result

    monkeypatch.setattr(chat.Runner, "run_streamed", run_streamed)
    captured.update(
        store=store,
        recorder=recorder,
        session_object=session,
        hooks_object=hooks,
        current_user=current_user,
        previous_user=previous_user,
        future_user=future_user,
    )
    return captured, popped, light_modes


def test_chat_runner_projects_openai_and_nested_events(monkeypatch) -> None:
    sdk_events = [
        SimpleNamespace(kind="text", value="Hello"),
        SimpleNamespace(
            kind="tool_start",
            tool_call_id="call-1",
            tool_name="show_canvas",
            input={"title": "Demo"},
        ),
        SimpleNamespace(
            kind="tool_end",
            tool_call_id="call-1",
            tool_name="show_canvas",
            output="{'url': '/demo'}",
        ),
    ]
    captured, popped, light_modes = _patch_runner_dependencies(
        monkeypatch, sdk_events
    )
    service = _RunsService()

    asyncio.run(chat.ChatRunner().execute(_run(), service))

    types = [event.event.type for event in service.events]
    assert types == [
        "started",
        "subagent_tool_event",
        "token",
        "tool_call_start",
        "tool_call_end",
        "completed",
    ]
    assert service.events[1].event.data == {
        "parentToolCallId": "parent-1",
        "childRunId": "child-1",
        "childEventType": "stream_start",
        "toolCallId": None,
        "toolName": None,
        "input": None,
        "output": None,
        "error": None,
    }
    assert service.events[4].event.data["output"] == {"url": "/demo"}
    current_input = captured["session_kwargs"]["current_input"]
    assert captured["input"] == [current_input]
    assert current_input == {
        "role": "user",
        "content": [{"type": "input_text", "text": "hello"}],
    }
    assert captured["session"] is captured["session_object"]
    assert captured["session_object"].added_items == [[current_input]]
    assert captured["hooks"] is captured["hooks_object"]
    assert captured["session_kwargs"] == {
        "store": captured["store"],
        "chat_id": "chat-1",
        "run_id": "run-1",
        "turn_id": "user-current",
        "current_user_message_id": "user-current",
        "current_input": current_input,
    }
    assert captured["recorder_kwargs"] == {
        "store": captured["store"],
        "chat_id": "chat-1",
        "run_id": "run-1",
        "turn_id": "user-current",
    }
    assert captured["persistence_order"] == [
        ("persist", sdk_events[0]),
        ("translate", sdk_events[0]),
        ("persist", sdk_events[1]),
        ("translate", sdk_events[1]),
        ("persist", sdk_events[2]),
        ("translate", sdk_events[2]),
    ]
    assert captured["recorder"].sdk_events == sdk_events
    assert len(captured["recorder"].custom_events) == 1
    assert captured["recorder"].application_events == [
        ("run.started", {"runId": "run-1", "turnId": "user-current"}),
        ("run.completed", {"runId": "run-1", "turnId": "user-current"}),
    ]
    assert captured["recorder"].finalize_count == 1
    assert captured["store"].calls[0] == (
        "create_turn",
        {
            "chat_id": "chat-1",
            "turn_id": "user-current",
            "user_message_id": "user-current",
            "run_id": "run-1",
        },
    )
    assert captured["store"].calls[1][0:2] == ("ensure_seeded", "chat-1")
    assert captured["store"].calls[1][2] == [
        (
            "user-previous",
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "Earlier question"}
                ],
            },
        )
    ]
    assert captured["store"].calls[2][0] == "append_items"
    assert captured["store"].calls[2][1]["source_message_id"] == "user-previous"
    assert captured["store"].calls[3:] == [
        ("set_turn_status", "user-current", "running"),
    ]
    assert captured["max_turns"] is None
    assert captured["run_config"].tracing_disabled is True
    assert captured["context"]._loop is not None
    assert popped
    assert light_modes == ["thinking", "idle"]


def test_chat_runner_cancels_and_closes_sdk_stream(monkeypatch) -> None:
    sdk_events = [
        SimpleNamespace(kind="text", value="partial"),
        SimpleNamespace(
            kind="tool_start",
            tool_call_id="call-1",
            tool_name="read",
            input={"path": "README.md"},
        ),
    ]
    captured, popped, light_modes = _patch_runner_dependencies(
        monkeypatch, sdk_events
    )
    service = _RunsService(cancel_on_token=True)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(chat.ChatRunner().execute(_run(), service))

    result = captured["result"]
    assert result.cancel_modes == ["immediate"]
    assert result.events.closed is True
    assert captured["persistence_order"] == [
        ("persist", sdk_events[0]),
        ("translate", sdk_events[0]),
        ("persist", sdk_events[1]),
    ]
    assert captured["recorder"].sdk_events == sdk_events
    assert captured["recorder"].application_events == [
        ("run.started", {"runId": "run-1", "turnId": "user-current"}),
        ("run.cancelled", {"runId": "run-1", "turnId": "user-current"}),
    ]
    assert captured["recorder"].finalize_count == 1
    assert captured["store"].calls[3:] == [
        ("set_turn_status", "user-current", "running"),
    ]
    assert "completed" not in [event.event.type for event in service.events]
    assert popped
    assert light_modes == ["thinking", "idle"]


def test_chat_runner_uses_final_output_when_no_text_delta_arrives(monkeypatch) -> None:
    _patch_runner_dependencies(monkeypatch, [], final_output="Fallback answer")
    service = _RunsService()

    asyncio.run(chat.ChatRunner().execute(_run(), service))

    assert [event.event.type for event in service.events] == [
        "started",
        "subagent_tool_event",
        "token",
        "completed",
    ]
    assert service.events[2].event.data == {"value": "Fallback answer"}
