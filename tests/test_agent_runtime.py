from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from aios_core.agent import runtime
from aios_core.agent.events import AgentEvent
from aios_core.agent.tools.subagent_events import build_subagent_stream_event
from server.types.chat import UserMessage


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


def _request() -> runtime.AgentRunRequest:
    return runtime.AgentRunRequest(
        run_id="run-1",
        chat_id="chat-1",
        turn_id="user-current",
    )


def _patch_runtime_dependencies(monkeypatch, sdk_events, *, final_output=None):
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
        runtime,
        "load_chat_session",
        lambda _chat_id: [previous_user, current_user, future_user],
    )
    monkeypatch.setattr(runtime, "create_agent", lambda **_kwargs: object())
    monkeypatch.setattr(
        runtime,
        "push_chat_runtime_context",
        lambda _chat_id: (object(), object(), object()),
    )
    popped = []
    monkeypatch.setattr(runtime, "pop_chat_runtime_context", popped.append)
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
    monkeypatch.setattr(runtime, "ConversationStore", lambda: store)

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

    monkeypatch.setattr(runtime, "ConversationRecorder", make_recorder)

    class Session:
        def __init__(self) -> None:
            self.added_items: list[list[dict[str, Any]]] = []

        async def add_items(self, items) -> None:
            self.added_items.append(items)

    session = Session()

    def make_session(**kwargs):
        captured["session_kwargs"] = kwargs
        return session

    monkeypatch.setattr(runtime, "CanonicalConversationSession", make_session)
    hooks = object()
    monkeypatch.setattr(runtime, "DurableRunHooks", lambda: hooks)

    def translate(event):
        captured["persistence_order"].append(("translate", event))
        return event

    monkeypatch.setattr(
        runtime,
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

    monkeypatch.setattr(runtime.Runner, "run_streamed", run_streamed)
    captured.update(
        store=store,
        recorder=recorder,
        session_object=session,
        hooks_object=hooks,
    )
    return captured, popped


async def _collect_events(agent_runtime: runtime.AgentRuntime) -> list[AgentEvent]:
    return [event async for event in agent_runtime.run(_request())]


def test_closing_stream_cancels_detached_emitters_before_they_requeue() -> None:
    class RuntimeWithDetachedEmitter(runtime.AgentRuntime):
        emitter: asyncio.Task[None] | None = None

        async def _run_serialized(self, request, emit, stream_state) -> None:
            del request
            del stream_state

            async def emit_many() -> None:
                await emit(AgentEvent(kind="started"))
                await emit(AgentEvent(kind="text_delta", value="too late"))

            self.emitter = asyncio.create_task(emit_many())
            try:
                await asyncio.Event().wait()
            finally:
                # Give the detached emitter the same teardown window used by
                # thread-originated tool events in the production runtime.
                await asyncio.sleep(0)

    async def consume_one_event() -> tuple[bool, bool]:
        agent_runtime = RuntimeWithDetachedEmitter()
        stream = agent_runtime.run(_request())
        assert (await anext(stream)).kind == "started"
        await stream.aclose()
        assert agent_runtime.emitter is not None
        await asyncio.sleep(0)
        return agent_runtime.emitter.done(), agent_runtime.emitter.cancelled()

    assert asyncio.run(consume_one_event()) == (True, True)


def test_cancelling_while_waiting_for_an_event_does_not_leak_queue_task() -> None:
    class RuntimeWithoutEvents(runtime.AgentRuntime):
        async def _run_serialized(self, request, emit, stream_state) -> None:
            del request
            del emit
            del stream_state
            await asyncio.Event().wait()

    async def cancel_waiting_consumer() -> list[asyncio.Task[Any]]:
        stream = RuntimeWithoutEvents().run(_request())
        consumer = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
        await asyncio.sleep(0)
        current = asyncio.current_task()
        return [
            task
            for task in asyncio.all_tasks()
            if task is not current and not task.done()
        ]

    assert asyncio.run(cancel_waiting_consumer()) == []


def test_consumer_failure_is_persisted_as_error_not_cancellation(monkeypatch) -> None:
    captured, _ = _patch_runtime_dependencies(
        monkeypatch,
        [AgentEvent(kind="text_delta", value="partial")],
    )

    async def fail_consumer() -> None:
        stream = runtime.AgentRuntime().run(_request())
        async for event in stream:
            if event.kind == "text_delta":
                try:
                    await stream.athrow(RuntimeError("projection failed"))
                except RuntimeError as exc:
                    assert str(exc) == "projection failed"
                break

    asyncio.run(fail_consumer())

    assert captured["result"].cancel_modes == ["immediate"]
    assert captured["recorder"].application_events == [
        ("run.started", {"runId": "run-1", "turnId": "user-current"}),
        (
            "run.error",
            {
                "runId": "run-1",
                "turnId": "user-current",
                "error": "projection failed",
            },
        ),
    ]


def test_agent_runtime_streams_and_persists_openai_and_nested_events(
    monkeypatch,
) -> None:
    sdk_events = [
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
    ]
    captured, popped = _patch_runtime_dependencies(monkeypatch, sdk_events)

    events = asyncio.run(_collect_events(runtime.AgentRuntime()))

    assert [event.kind for event in events] == [
        "started",
        "subagent_tool_event",
        "text_delta",
        "tool_call_start",
        "tool_call_end",
        "completed",
    ]
    assert events[1] == AgentEvent(
        kind="subagent_tool_event",
        parent_tool_call_id="parent-1",
        child_run_id="child-1",
        child_event_type="stream_start",
    )
    assert events[4].output == {"url": "/demo"}
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
                "content": [{"type": "input_text", "text": "Earlier question"}],
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


def test_closing_agent_stream_cancels_and_drains_sdk_stream(monkeypatch) -> None:
    sdk_events = [
        AgentEvent(kind="text_delta", value="partial"),
        AgentEvent(
            kind="tool_call_start",
            tool_call_id="call-1",
            tool_name="read",
            input={"path": "README.md"},
        ),
    ]
    captured, popped = _patch_runtime_dependencies(monkeypatch, sdk_events)

    async def consume_then_close() -> list[AgentEvent]:
        stream = runtime.AgentRuntime().run(_request())
        seen: list[AgentEvent] = []
        async for event in stream:
            seen.append(event)
            if event.kind == "text_delta":
                await stream.aclose()
                break
        return seen

    events = asyncio.run(consume_then_close())

    result = captured["result"]
    assert result.cancel_modes == ["immediate"]
    assert result.events.closed is True
    assert [event.kind for event in events] == [
        "started",
        "subagent_tool_event",
        "text_delta",
    ]
    assert captured["recorder"].sdk_events == sdk_events
    assert captured["recorder"].application_events == [
        ("run.started", {"runId": "run-1", "turnId": "user-current"}),
        ("run.cancelled", {"runId": "run-1", "turnId": "user-current"}),
    ]
    assert captured["recorder"].finalize_count == 1
    assert popped


def test_agent_runtime_uses_final_output_when_no_text_delta_arrives(
    monkeypatch,
) -> None:
    _patch_runtime_dependencies(monkeypatch, [], final_output="Fallback answer")

    events = asyncio.run(_collect_events(runtime.AgentRuntime()))

    assert [event.kind for event in events] == [
        "started",
        "subagent_tool_event",
        "text_delta",
        "completed",
    ]
    assert events[2].value == "Fallback answer"


def test_background_invocation_stays_behind_agent_runtime(monkeypatch) -> None:
    agent = object()
    captured: dict[str, Any] = {}
    monkeypatch.setattr(runtime, "create_agent", lambda **kwargs: agent)

    def run_sync(selected_agent, input, **kwargs):
        captured.update(agent=selected_agent, input=input, **kwargs)
        return SimpleNamespace(final_output="scheduled result")

    monkeypatch.setattr(runtime.Runner, "run_sync", run_sync)

    output = runtime.run_agent_to_completion(
        [{"role": "user", "content": "scheduled work"}]
    )

    assert output == "scheduled result"
    assert captured["agent"] is agent
    assert captured["max_turns"] is None
    assert captured["run_config"].tracing_disabled is True
