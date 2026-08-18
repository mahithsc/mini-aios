from __future__ import annotations

import asyncio
import threading

from agents import Agent
from agents.items import ToolCallItem, ToolCallOutputItem
from agents.stream_events import RawResponsesStreamEvent, RunItemStreamEvent
from agents.tool_context import ToolContext
from openai.types.responses import ResponseFunctionToolCall, ResponseTextDeltaEvent

from aios_core.agent.context import AgentRuntimeContext
from aios_core.agent.openai import OpenAIEventTranslator, as_function_tool
from aios_core.agent.tools.subagent_events import build_subagent_stream_event


def _context(runtime: AgentRuntimeContext, arguments: str) -> ToolContext:
    return ToolContext(
        context=runtime,
        tool_name="sample",
        tool_call_id="call-123",
        tool_arguments=arguments,
    )


def test_function_tool_hides_fc_and_injects_call_id() -> None:
    seen: list[tuple[str, str]] = []

    def sample(value: str, fc=None) -> str:
        seen.append((value, fc.call_id))
        return value.upper()

    async def invoke() -> str:
        runtime = AgentRuntimeContext()
        tool = as_function_tool(sample)
        assert set(tool.params_json_schema["properties"]) == {"value"}
        return await tool.on_invoke_tool(
            _context(runtime, '{"value":"minimal"}'),
            '{"value":"minimal"}',
        )

    assert asyncio.run(invoke()) == "MINIMAL"
    assert seen == [("minimal", "call-123")]


def test_generator_tool_forwards_events_and_returns_only_text() -> None:
    def sample(task: str, fc=None):
        yield build_subagent_stream_event(
            parent_tool_call_id=fc.call_id,
            child_run_id="child",
            child_event_type="stream_start",
        )
        yield f"done: {task}"

    async def invoke() -> tuple[str, list[tuple[str, str]]]:
        seen: list[tuple[str, str]] = []

        async def sink(event) -> None:
            seen.append((event.parent_tool_call_id, event.child_event_type))

        runtime = AgentRuntimeContext(event_sink=sink)
        runtime.bind_to_current_loop()
        tool = as_function_tool(sample)
        output = await tool.on_invoke_tool(
            _context(runtime, '{"task":"work"}'),
            '{"task":"work"}',
        )
        return output, seen

    output, seen = asyncio.run(invoke())
    assert output == "done: work"
    assert seen == [("call-123", "stream_start")]


def test_cancelling_generator_tool_stops_cleanup_and_future_events() -> None:
    released = threading.Event()
    stopped = threading.Event()

    def sample(fc=None):
        fc.add_cancel_callback(released.set)
        try:
            yield build_subagent_stream_event(
                parent_tool_call_id=fc.call_id,
                child_run_id="child",
                child_event_type="stream_start",
            )
            released.wait(timeout=5)
            yield build_subagent_stream_event(
                parent_tool_call_id=fc.call_id,
                child_run_id="child",
                child_event_type="tool_call_start",
            )
        finally:
            stopped.set()

    async def invoke() -> list[str]:
        seen: list[str] = []
        first_event = asyncio.Event()

        async def sink(event) -> None:
            seen.append(event.child_event_type)
            first_event.set()

        runtime = AgentRuntimeContext(event_sink=sink)
        runtime.bind_to_current_loop()
        tool = as_function_tool(sample)
        task = asyncio.create_task(
            tool.on_invoke_tool(
                _context(runtime, "{}"),
                "{}",
            )
        )
        await asyncio.wait_for(first_event.wait(), timeout=1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert await asyncio.to_thread(stopped.wait, 1)
        return seen

    assert asyncio.run(invoke()) == ["stream_start"]


def test_translator_correlates_text_and_tool_lifecycle() -> None:
    translator = OpenAIEventTranslator()
    text = translator.translate(
        RawResponsesStreamEvent(
            data=ResponseTextDeltaEvent(
                content_index=0,
                delta="hello",
                item_id="message-1",
                logprobs=[],
                output_index=0,
                sequence_number=1,
                type="response.output_text.delta",
            )
        )
    )
    assert text is not None
    assert (text.kind, text.value) == ("text_delta", "hello")

    agent = Agent(name="test")
    called = translator.translate(
        RunItemStreamEvent(
            name="tool_called",
            item=ToolCallItem(
                agent=agent,
                raw_item=ResponseFunctionToolCall(
                    arguments='{"path":"README.md"}',
                    call_id="call-1",
                    name="read",
                    type="function_call",
                ),
            ),
        )
    )
    assert called is not None
    assert called.kind == "tool_call_start"
    assert called.tool_call_id == "call-1"
    assert called.tool_name == "read"
    assert called.input == {"path": "README.md"}

    completed = translator.translate(
        RunItemStreamEvent(
            name="tool_output",
            item=ToolCallOutputItem(
                agent=agent,
                raw_item={"type": "function_call_output", "call_id": "call-1"},
                output="contents",
            ),
        )
    )
    assert completed is not None
    assert completed.kind == "tool_call_end"
    assert completed.tool_call_id == "call-1"
    assert completed.tool_name == "read"
    assert completed.output == "contents"
