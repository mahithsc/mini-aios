from __future__ import annotations

import asyncio
from types import SimpleNamespace

from agno.agent import RunEvent as AgentRunEvent

from server.execution.runners import chat as chat_runner
from server.types.run import Run, RunEvent


class FakeAgent:
    def __init__(self) -> None:
        self.closed = False

    async def arun(self, *_args, **_kwargs):
        try:
            yield SimpleNamespace(
                content="Hello",
                event=AgentRunEvent.run_content,
            )
            yield SimpleNamespace(
                content="Hello",
                event=AgentRunEvent.run_content_completed,
            )
            await asyncio.Event().wait()
        finally:
            self.closed = True


class FakeAgentWithOpenTransport:
    def __init__(self) -> None:
        self.closed = False

    async def arun(self, *_args, **_kwargs):
        try:
            yield SimpleNamespace(
                content="Complete response",
                event=AgentRunEvent.run_content,
            )
            while True:
                await asyncio.sleep(0.002)
                yield SimpleNamespace(
                    content="",
                    event=AgentRunEvent.run_content,
                )
        finally:
            self.closed = True


class FakeLights:
    async def set_mode(self, _mode: str) -> None:
        return None


class RecordingRunsService:
    def __init__(self) -> None:
        self.events: list[RunEvent] = []

    async def emit_event(self, _run_id: str, event: RunEvent) -> RunEvent:
        self.events.append(event)
        return event


def test_content_completion_finishes_a_run_before_agent_cleanup(
    monkeypatch,
) -> None:
    agent = FakeAgent()
    runs_service = RecordingRunsService()
    run = Run(
        id="run-1",
        kind="chat",
        status="running",
        createdAt=1,
        updatedAt=1,
        chatId="chat-1",
    )
    monkeypatch.setattr(chat_runner, "create_agent", lambda **_kwargs: agent)
    monkeypatch.setattr(chat_runner, "lights", FakeLights())
    monkeypatch.setattr(chat_runner, "load_chat_session", lambda _chat_id: [])

    asyncio.run(chat_runner.ChatRunner().execute(run, runs_service))

    assert [event.event.type for event in runs_service.events] == [
        "started",
        "token",
        "completed",
    ]
    assert agent.closed is True


def test_idle_transport_after_content_finishes_the_run(monkeypatch) -> None:
    agent = FakeAgentWithOpenTransport()
    runs_service = RecordingRunsService()
    run = Run(
        id="run-2",
        kind="chat",
        status="running",
        createdAt=1,
        updatedAt=1,
        chatId="chat-2",
    )
    monkeypatch.setattr(chat_runner, "create_agent", lambda **_kwargs: agent)
    monkeypatch.setattr(chat_runner, "lights", FakeLights())
    monkeypatch.setattr(chat_runner, "load_chat_session", lambda _chat_id: [])
    monkeypatch.setattr(
        chat_runner,
        "AGENT_STREAM_IDLE_COMPLETION_SECONDS",
        0.01,
    )

    asyncio.run(chat_runner.ChatRunner().execute(run, runs_service))

    assert [event.event.type for event in runs_service.events] == [
        "started",
        "token",
        "completed",
    ]
    assert agent.closed is True
