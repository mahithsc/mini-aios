"""In-process delegated agent tool."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from uuid import uuid4

from agents import RunConfig, Runner

from ..openai_runtime import OpenAIEventTranslator
from ..prompt_loader import render_prompt
from .subagent_events import (
    SubagentStreamEvent,
    build_subagent_stream_event as _build_subagent_stream_event,
)

__all__ = ["subagent", "SubagentStreamEvent"]


async def subagent(task: str | None = None, timeout: float = 60, fc=None) -> str:
    """Run one focused delegated task and stream its tool activity to the UI."""
    if timeout is None:
        return "error: timeout must be > 0"
    try:
        timeout_value = float(timeout)
    except (TypeError, ValueError):
        return "error: timeout must be a number"
    if timeout_value <= 0:
        return "error: timeout must be > 0"
    if not isinstance(task, str) or not task.strip():
        return "error: task is required"

    from aios_core.agent import create_subagent_worker

    child_run_id = str(uuid4())
    parent_tool_call_id = str(getattr(fc, "call_id", None) or child_run_id)
    runtime_context = getattr(fc, "runtime_context", None)
    prompt = render_prompt("subagent.md", task=task.strip())

    async def emit(child_event_type: str, **kwargs: object) -> None:
        if runtime_context is None:
            return
        await runtime_context.emit(
            _build_subagent_stream_event(
                parent_tool_call_id=parent_tool_call_id,
                child_run_id=child_run_id,
                child_event_type=child_event_type,
                **kwargs,
            )
        )

    await emit("stream_start")
    translator = OpenAIEventTranslator()
    chunks: list[str] = []
    result = Runner.run_streamed(
        create_subagent_worker(),
        [{"role": "user", "content": prompt}],
        context=runtime_context,
        max_turns=None,
        run_config=RunConfig(tracing_disabled=True),
    )
    events = result.stream_events()

    try:
        async with asyncio.timeout(timeout_value):
            async for sdk_event in events:
                event = translator.translate(sdk_event)
                if event is None:
                    continue
                if event.kind == "text" and event.value is not None:
                    chunks.append(event.value)
                elif event.kind == "tool_start":
                    await emit(
                        "tool_call_start",
                        tool_call_id=event.tool_call_id,
                        tool_name=event.tool_name,
                        input=event.input,
                    )
                elif event.kind == "tool_end":
                    await emit(
                        "tool_call_end",
                        tool_call_id=event.tool_call_id,
                        tool_name=event.tool_name,
                        output=event.output,
                    )
        if result.run_loop_exception is not None:
            raise result.run_loop_exception
    except TimeoutError:
        result.cancel(mode="immediate")
        with suppress(BaseException):
            await events.aclose()
        message = f"Subagent timed out after {timeout_value:g}s."
        await emit("stream_error", error=message)
        return f"error: subagent timed out after {timeout_value:g}s"
    except asyncio.CancelledError:
        result.cancel(mode="immediate")
        with suppress(BaseException):
            await events.aclose()
        raise
    except Exception as exc:
        result.cancel(mode="immediate")
        with suppress(BaseException):
            await events.aclose()
        await emit("stream_error", error=str(exc))
        return f"error: subagent failed -- {exc}"

    await emit("stream_end")
    if chunks:
        return "".join(chunks).strip() or "(empty)"
    return str(result.final_output or "(empty)")
