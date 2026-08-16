from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from aios_core.tools.subagent_events import build_subagent_stream_event
from server.execution.runners import chat
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
    )


def _patch_runner_dependencies(monkeypatch, sdk_events, *, final_output=None):
    monkeypatch.setattr(chat, "load_chat_session", lambda _chat_id: [])
    monkeypatch.setattr(
        chat,
        "format_chat_messages_to_model_messages",
        lambda _messages: [{"role": "user", "content": "hello"}],
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
    monkeypatch.setattr(
        chat,
        "OpenAIEventTranslator",
        lambda: SimpleNamespace(translate=lambda event: event),
    )

    captured = {}

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
