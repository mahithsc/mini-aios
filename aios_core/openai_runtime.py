"""Small compatibility boundary between AIOS and the OpenAI Agents SDK.

AIOS owns its tools and public streaming-event contract.  This module keeps the
SDK-specific context injection, generator draining, and event translation in
one place so neither the tools nor the server need to depend on SDK internals.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import threading
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from functools import wraps
from typing import Any, Literal, get_type_hints

from agents import FunctionTool, function_tool
from agents.items import ToolCallItem, ToolCallOutputItem
from agents.tool_context import ToolContext
from openai.types.responses import ResponseTextDeltaEvent

from .tools.subagent_events import SubagentStreamEvent

EventSink = Callable[[SubagentStreamEvent], Awaitable[None] | None]


class AgentRuntimeContext:
    """Per-run application context shared with function tools."""

    def __init__(self, event_sink: EventSink | None = None) -> None:
        self._event_sink = event_sink
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_to_current_loop(self) -> None:
        """Bind thread-originated generator events to the active run loop."""
        self._loop = asyncio.get_running_loop()

    async def emit(self, event: SubagentStreamEvent) -> None:
        if self._event_sink is None:
            return
        result = self._event_sink(event)
        if inspect.isawaitable(result):
            await result

    def emit_sync(self, event: SubagentStreamEvent) -> None:
        """Forward an event from a synchronous tool's worker thread."""
        if self._event_sink is None:
            return
        loop = self._loop
        if loop is None or not loop.is_running():
            return
        future = asyncio.run_coroutine_threadsafe(self.emit(event), loop)
        future.result()


class FunctionCallContext:
    """Minimal compatibility object for existing tools that accept ``fc``."""

    def __init__(self, call_id: str, runtime_context: AgentRuntimeContext) -> None:
        self.call_id = call_id
        self.runtime_context = runtime_context
        self._cancelled = threading.Event()
        self._cancel_callbacks: list[Callable[[], None]] = []
        self._cancel_lock = threading.Lock()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def add_cancel_callback(self, callback: Callable[[], None]) -> None:
        """Register synchronous cleanup for a thread-backed tool."""
        with self._cancel_lock:
            if not self._cancelled.is_set():
                self._cancel_callbacks.append(callback)
                return
        with suppress(Exception):
            callback()

    def cancel(self) -> None:
        with self._cancel_lock:
            if self._cancelled.is_set():
                return
            self._cancelled.set()
            callbacks = list(self._cancel_callbacks)
            self._cancel_callbacks.clear()
        for callback in callbacks:
            with suppress(Exception):
                callback()


def _resolved_signature(function: Callable[..., Any]) -> inspect.Signature:
    signature = inspect.signature(function)
    try:
        hints = get_type_hints(function)
    except Exception:
        hints = {}

    parameters: list[inspect.Parameter] = [
        inspect.Parameter(
            "ctx",
            kind=inspect.Parameter.POSITIONAL_OR_KEYWORD,
            annotation=ToolContext,
        )
    ]
    for parameter in signature.parameters.values():
        if parameter.name == "fc":
            continue
        annotation = hints.get(parameter.name, parameter.annotation)
        if parameter.default is None and annotation not in {
            inspect.Parameter.empty,
            Any,
        }:
            try:
                annotation = annotation | None
            except TypeError:
                pass
        parameters.append(parameter.replace(annotation=annotation))

    return inspect.Signature(parameters, return_annotation=Any)


def _drain_generator(
    result: Any,
    runtime_context: AgentRuntimeContext,
    call_context: FunctionCallContext,
) -> Any:
    if not inspect.isgenerator(result):
        return result

    output: list[str] = []
    while not call_context.cancelled:
        try:
            item = next(result)
        except StopIteration:
            break
        if call_context.cancelled:
            break
        if isinstance(item, SubagentStreamEvent):
            runtime_context.emit_sync(item)
        elif item is not None:
            output.append(str(item))
    if call_context.cancelled:
        with suppress(BaseException):
            result.close()
    return "".join(output)


def as_function_tool(
    function: Callable[..., Any],
    *,
    strict_mode: bool = True,
) -> FunctionTool:
    """Expose an AIOS callable as an SDK tool without leaking hidden ``fc``.

    Synchronous generator tools are drained on the SDK worker thread. Their
    project-owned nested events are forwarded live and only ordinary yields are
    concatenated into the model-visible tool result.
    """

    original_signature = inspect.signature(function)
    accepts_fc = "fc" in original_signature.parameters
    public_signature = _resolved_signature(function)

    @wraps(function)
    async def invoke(ctx: ToolContext, *args: Any, **kwargs: Any) -> Any:
        runtime_context = (
            ctx.context
            if isinstance(ctx.context, AgentRuntimeContext)
            else AgentRuntimeContext()
        )
        bound = public_signature.bind(ctx, *args, **kwargs)
        call_kwargs = {
            name: value
            for name, value in bound.arguments.items()
            if name != "ctx"
        }
        call_context = FunctionCallContext(
            call_id=str(ctx.tool_call_id),
            runtime_context=runtime_context,
        )
        if accepts_fc:
            call_kwargs["fc"] = call_context

        if inspect.iscoroutinefunction(function):
            try:
                return await function(**call_kwargs)
            except asyncio.CancelledError:
                call_context.cancel()
                raise

        def call_sync() -> Any:
            return _drain_generator(
                function(**call_kwargs),
                runtime_context,
                call_context,
            )

        worker = asyncio.create_task(asyncio.to_thread(call_sync))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            call_context.cancel()

            def consume_result(task: asyncio.Task[Any]) -> None:
                with suppress(BaseException):
                    task.result()

            worker.add_done_callback(consume_result)
            raise

    invoke.__signature__ = public_signature  # type: ignore[attr-defined]
    invoke.__annotations__ = {
        parameter.name: parameter.annotation
        for parameter in public_signature.parameters.values()
        if parameter.annotation is not inspect.Parameter.empty
    }
    invoke.__annotations__["return"] = Any
    return function_tool(invoke, strict_mode=strict_mode)


@dataclass(frozen=True)
class AgentEvent:
    kind: Literal["text", "tool_start", "tool_end"]
    value: str | None = None
    tool_call_id: str | None = None
    tool_name: str = "tool"
    input: object | None = None
    output: object | None = None


def _raw_arguments(item: ToolCallItem) -> object | None:
    raw_item = item.raw_item
    if isinstance(raw_item, dict):
        return raw_item.get("arguments")
    return getattr(raw_item, "arguments", None)


def _parse_arguments(arguments: object | None) -> object:
    if arguments is None or arguments == "":
        return {}
    if not isinstance(arguments, str):
        return arguments
    try:
        return json.loads(arguments)
    except json.JSONDecodeError:
        return arguments


class OpenAIEventTranslator:
    """Translate SDK stream objects into AIOS's stable three-event core."""

    def __init__(self) -> None:
        self._tool_names: dict[str, str] = {}

    def translate(self, event: object) -> AgentEvent | None:
        event_type = getattr(event, "type", None)
        if event_type == "raw_response_event":
            data = getattr(event, "data", None)
            if isinstance(data, ResponseTextDeltaEvent):
                return AgentEvent(kind="text", value=data.delta)
            return None

        if event_type != "run_item_stream_event":
            return None

        name = getattr(event, "name", None)
        item = getattr(event, "item", None)
        if name == "tool_called" and isinstance(item, ToolCallItem):
            call_id = str(item.call_id or id(item))
            tool_name = str(item.tool_name or "tool")
            self._tool_names[call_id] = tool_name
            return AgentEvent(
                kind="tool_start",
                tool_call_id=call_id,
                tool_name=tool_name,
                input=_parse_arguments(_raw_arguments(item)),
            )

        if name == "tool_output" and isinstance(item, ToolCallOutputItem):
            call_id = str(item.call_id or id(item))
            tool_name = self._tool_names.pop(call_id, None)
            if tool_name is None:
                origin = getattr(item, "tool_origin", None)
                tool_name = getattr(origin, "agent_tool_name", None) or "tool"
            return AgentEvent(
                kind="tool_end",
                tool_call_id=call_id,
                tool_name=str(tool_name),
                output=item.output,
            )

        return None


__all__ = [
    "AgentEvent",
    "AgentRuntimeContext",
    "FunctionCallContext",
    "OpenAIEventTranslator",
    "as_function_tool",
]
