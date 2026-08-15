from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
from agno.models.message import Message
from agno.models.response import ModelResponse

from aios_core import openai_chat
from aios_core.openai_chat import AiosOpenAIChat


class HangingAfterFinishStream:
    def __init__(self) -> None:
        self.closed = False
        self.index = 0
        self.chunks = [
            SimpleNamespace(
                choices=[SimpleNamespace(finish_reason=None)],
                marker="content",
            ),
            SimpleNamespace(
                choices=[SimpleNamespace(finish_reason="stop")],
                marker="finished",
            ),
        ]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.index < len(self.chunks):
            chunk = self.chunks[self.index]
            self.index += 1
            return chunk

        await asyncio.Event().wait()
        raise StopAsyncIteration


class EmptyHeartbeatStream:
    def __init__(self) -> None:
        self.closed = False
        self.sent_content = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.sent_content:
            self.sent_content = True
            return SimpleNamespace(
                choices=[SimpleNamespace(finish_reason=None)],
                marker="content",
            )

        await asyncio.sleep(0.002)
        return SimpleNamespace(
            choices=[],
            marker=None,
        )


class ReadTimeoutAfterContentStream(EmptyHeartbeatStream):
    async def __anext__(self):
        if not self.sent_content:
            self.sent_content = True
            return SimpleNamespace(
                choices=[SimpleNamespace(finish_reason=None)],
                marker="content",
            )

        raise httpx.ReadTimeout("stream stayed open after final content")


class FakeCompletions:
    def __init__(self, stream: HangingAfterFinishStream) -> None:
        self.stream = stream

    async def create(self, **_kwargs):
        return self.stream


class FakeOpenAIClient:
    def __init__(self, stream: HangingAfterFinishStream) -> None:
        self.chat = SimpleNamespace(completions=FakeCompletions(stream))


class FakeAiosOpenAIChat(AiosOpenAIChat):
    def __init__(self, stream: HangingAfterFinishStream) -> None:
        super().__init__(id="test-model", api_key="test-key")
        self.fake_client = FakeOpenAIClient(stream)

    def get_async_client(self):
        return self.fake_client

    def _parse_provider_response_delta(self, chunk):
        return ModelResponse(content=chunk.marker)


def test_stream_stops_at_finish_reason_even_when_transport_stays_open() -> None:
    stream = HangingAfterFinishStream()
    model = FakeAiosOpenAIChat(stream)

    async def collect() -> list[str]:
        return [
            event
            async for event in model.ainvoke_stream(
                [Message(role="user", content="Hello")],
                Message(role="assistant"),
            )
        ]

    events = asyncio.run(asyncio.wait_for(collect(), timeout=1))

    assert [event.content for event in events] == ["content", "finished"]
    assert stream.closed is True


def test_stream_stops_after_empty_heartbeats(monkeypatch) -> None:
    stream = EmptyHeartbeatStream()
    model = FakeAiosOpenAIChat(stream)
    monkeypatch.setattr(
        openai_chat,
        "MODEL_STREAM_IDLE_COMPLETION_SECONDS",
        0.01,
    )

    async def collect() -> list[ModelResponse]:
        return [
            event
            async for event in model.ainvoke_stream(
                [Message(role="user", content="Hello")],
                Message(role="assistant"),
            )
        ]

    events = asyncio.run(asyncio.wait_for(collect(), timeout=1))

    assert events[0].content == "content"
    assert stream.closed is True


def test_read_timeout_after_content_is_treated_as_completion() -> None:
    stream = ReadTimeoutAfterContentStream()
    model = FakeAiosOpenAIChat(stream)

    async def collect() -> list[ModelResponse]:
        return [
            event
            async for event in model.ainvoke_stream(
                [Message(role="user", content="Hello")],
                Message(role="assistant"),
            )
        ]

    events = asyncio.run(asyncio.wait_for(collect(), timeout=1))

    assert [event.content for event in events] == ["content"]
    assert stream.closed is True
