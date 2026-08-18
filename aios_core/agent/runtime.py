"""The streamed AIOS agent loop and its conversation-persistence policy."""

from __future__ import annotations

import asyncio
import weakref
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from typing import Any

from agents import RunConfig, Runner
from agents.result import RunResultStreaming
from agents.stream_events import StreamEvent

from ..conversation_store import MAIN_SCOPE, ConversationStore
from ..sessions import load_chat_session
from .context import (
    AgentRuntimeContext,
    pop_chat_runtime_context,
    push_chat_runtime_context,
)
from .events import AgentEvent, normalize_tool_output
from .factory import create_agent
from .messages import (
    chat_message_to_model_message,
    legacy_chat_message_to_model_item,
)
from .openai import OpenAIEventTranslator
from .persistence import (
    CanonicalConversationSession,
    ConversationRecorder,
    DurableRunHooks,
)

AgentEventSink = Callable[[AgentEvent], Awaitable[None]]

_CHAT_LOCKS: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[str, asyncio.Lock]
] = weakref.WeakKeyDictionary()


@dataclass(frozen=True, slots=True)
class AgentRunRequest:
    run_id: str
    chat_id: str
    turn_id: str | None = None


@dataclass(frozen=True, slots=True)
class _QueuedAgentEvent:
    event: AgentEvent
    acknowledged: asyncio.Future[None]


@dataclass(slots=True)
class _RunStreamState:
    consumer_error: Exception | None = None


def _chat_lock(chat_id: str) -> asyncio.Lock:
    """Serialize the load/model/save interval for each chat and event loop."""
    loop = asyncio.get_running_loop()
    locks = _CHAT_LOCKS.setdefault(loop, {})
    return locks.setdefault(chat_id, asyncio.Lock())


def _is_user_message(message: object) -> bool:
    return getattr(message, "role", None) == "user"


def _current_user_message(
    messages: list[Any],
    turn_id: str | None,
) -> tuple[int, Any]:
    if turn_id:
        for index, message in enumerate(messages):
            if _is_user_message(message) and getattr(message, "id", None) == turn_id:
                return index, message
        raise RuntimeError(f"User message {turn_id} was not found in this chat.")

    # Compatibility for queued runs created before turn IDs were attached.
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if _is_user_message(message):
            return index, message
    raise RuntimeError("Chat run has no user message to process.")


async def _stop_stream(
    result: RunResultStreaming | None,
    events: AsyncIterator[StreamEvent] | None,
    record_event: Callable[[StreamEvent], Awaitable[object]] | None = None,
) -> None:
    """Cancel the SDK background run and drain its native event iterator."""
    if result is not None and not result.is_complete:
        result.cancel(mode="immediate")

    if events is None:
        return

    try:
        async for event in events:
            if record_event is not None:
                with suppress(BaseException):
                    await record_event(event)
    except BaseException:
        # Cleanup must never replace the original cancellation or run error.
        pass
    finally:
        with suppress(BaseException):
            await events.aclose()


class AgentRuntime:
    """Run one agent turn and expose only AIOS-owned stream events.

    The runtime decides when conversation and tool lifecycle data is stored.
    ``ConversationStore`` remains responsible for the SQLite implementation.
    """

    def __init__(
        self,
        *,
        store_factory: Callable[[], ConversationStore] | None = None,
        transcript_loader: Callable[[str], list[Any]] | None = None,
        agent_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._store_factory = store_factory or ConversationStore
        self._transcript_loader = transcript_loader or load_chat_session
        self._agent_factory = agent_factory or create_agent

    async def run(self, request: AgentRunRequest) -> AsyncIterator[AgentEvent]:
        """Yield a backpressured, provider-neutral event stream for one turn."""
        queue: asyncio.Queue[_QueuedAgentEvent] = asyncio.Queue()
        pending_acknowledgements: set[asyncio.Future[None]] = set()
        stream_state = _RunStreamState()
        accepting_events = True

        async def enqueue(event: AgentEvent) -> None:
            if not accepting_events:
                raise asyncio.CancelledError
            acknowledged = asyncio.get_running_loop().create_future()
            pending_acknowledgements.add(acknowledged)
            try:
                if not accepting_events:
                    raise asyncio.CancelledError
                queue.put_nowait(_QueuedAgentEvent(event, acknowledged))
                await acknowledged
            finally:
                pending_acknowledgements.discard(acknowledged)

        def cancel_pending_acknowledgements() -> None:
            for acknowledged in tuple(pending_acknowledgements):
                if not acknowledged.done():
                    acknowledged.cancel()

        producer = asyncio.create_task(
            self._run_serialized(request, enqueue, stream_state),
            name=f"agent-run-{request.run_id}",
        )
        next_event: asyncio.Task[_QueuedAgentEvent] | None = None

        try:
            while True:
                if not queue.empty():
                    queued = queue.get_nowait()
                    try:
                        yield queued.event
                    finally:
                        if not queued.acknowledged.done():
                            queued.acknowledged.set_result(None)
                    continue
                if producer.done():
                    break

                next_event = asyncio.create_task(queue.get())
                done, _ = await asyncio.wait(
                    {next_event, producer},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if next_event in done:
                    queued = next_event.result()
                    next_event = None
                    try:
                        yield queued.event
                    finally:
                        if not queued.acknowledged.done():
                            queued.acknowledged.set_result(None)
                    continue

                next_event.cancel()
                await asyncio.gather(next_event, return_exceptions=True)
                next_event = None

            await producer
        except Exception as exc:
            # ``ChatRunner`` injects projection/broadcast failures with
            # ``athrow``. Keep that distinct from cancellation so the runtime
            # persists the correct terminal conversation status.
            if not producer.done():
                stream_state.consumer_error = exc
            raise
        finally:
            # Stop detached tool emitters before acknowledging the event that
            # caused the consumer to close. Without this gate, an emitter can
            # resume during producer teardown, enqueue one more event, and wait
            # forever because there is no longer a stream consumer.
            accepting_events = False
            cancel_pending_acknowledgements()
            if next_event is not None:
                if not next_event.done():
                    next_event.cancel()
                with suppress(BaseException):
                    await asyncio.gather(next_event, return_exceptions=True)
            if not producer.done():
                producer.cancel()
            with suppress(BaseException):
                await producer
            cancel_pending_acknowledgements()

    async def _run_serialized(
        self,
        request: AgentRunRequest,
        emit: AgentEventSink,
        stream_state: _RunStreamState,
    ) -> None:
        if not request.chat_id:
            await emit(
                AgentEvent(kind="error", error="Agent run is missing a chat ID.")
            )
            return
        async with _chat_lock(request.chat_id):
            await self._run_turn(request, emit, stream_state)

    async def _run_turn(
        self,
        request: AgentRunRequest,
        emit: AgentEventSink,
        stream_state: _RunStreamState,
    ) -> None:
        produced_output = False
        produced_text = False
        context_tokens: tuple[Any, ...] | None = None
        stream_result: RunResultStreaming | None = None
        stream_events: AsyncIterator[StreamEvent] | None = None
        store: ConversationStore | None = None
        recorder: ConversationRecorder | None = None
        runtime_context: AgentRuntimeContext | None = None
        turn_id: str | None = None

        async def emit_nested_event(event: Any) -> None:
            nonlocal produced_output
            produced_output = True
            tool_name = getattr(event, "tool_name", None)
            await emit(
                AgentEvent(
                    kind="subagent_tool_event",
                    parent_tool_call_id=getattr(event, "parent_tool_call_id", None),
                    child_run_id=getattr(event, "child_run_id", None),
                    child_event_type=getattr(event, "child_event_type", None),
                    tool_call_id=getattr(event, "tool_call_id", None),
                    tool_name=tool_name,
                    input=getattr(event, "input", None),
                    output=normalize_tool_output(
                        tool_name,
                        getattr(event, "output", None),
                    ),
                    error=getattr(event, "error", None),
                )
            )

        try:
            transcript = self._transcript_loader(request.chat_id)
            current_index, current_user = _current_user_message(
                transcript,
                request.turn_id,
            )
            current_user_id = str(current_user.id)
            turn_id = request.turn_id or current_user_id
            current_input = chat_message_to_model_message(current_user)
            legacy_seed: list[tuple[str, Any]] = []
            for message in transcript[:current_index]:
                seed_item = legacy_chat_message_to_model_item(message)
                if seed_item is not None:
                    legacy_seed.append((str(message.id), seed_item))

            store = self._store_factory()
            await asyncio.to_thread(
                store.create_turn,
                chat_id=request.chat_id,
                turn_id=turn_id,
                user_message_id=current_user_id,
                run_id=request.run_id,
            )
            await asyncio.to_thread(store.ensure_seeded, request.chat_id, legacy_seed)

            # Reconcile user rows that were committed while an earlier run was
            # queued or cancelled. Provider-owned assistant/tool rows are never
            # reconstructed from the desktop projection.
            for message in transcript[:current_index]:
                if not _is_user_message(message):
                    continue
                await asyncio.to_thread(
                    store.append_items,
                    chat_id=request.chat_id,
                    scope_key=MAIN_SCOPE,
                    run_id=None,
                    turn_id=None,
                    items=[chat_message_to_model_message(message)],
                    source="ui_message",
                    replayable=True,
                    source_message_id=str(message.id),
                    dedupe_prefix="runner-reconcile",
                )

            await asyncio.to_thread(store.set_turn_status, turn_id, "running")
            recorder = ConversationRecorder(
                store=store,
                chat_id=request.chat_id,
                run_id=request.run_id,
                turn_id=turn_id,
            )
            await recorder.record_application_event(
                "run.started",
                {"runId": request.run_id, "turnId": turn_id},
            )

            session = CanonicalConversationSession(
                store=store,
                chat_id=request.chat_id,
                run_id=request.run_id,
                turn_id=turn_id,
                current_user_message_id=current_user_id,
                current_input=current_input,
            )
            # Persist the user input before provider setup. The SDK's second
            # write is idempotent because it carries the same source identity.
            await session.add_items([current_input])
            runtime_context = AgentRuntimeContext(
                event_sink=emit_nested_event,
                conversation_recorder=recorder,
            )
            runtime_context.bind_to_current_loop()
            translator = OpenAIEventTranslator()
            context_tokens = push_chat_runtime_context(request.chat_id)

            await emit(AgentEvent(kind="started"))
            agent = self._agent_factory(chat_id=request.chat_id)
            stream_result = Runner.run_streamed(
                agent,
                input=[current_input],
                context=runtime_context,
                max_turns=None,
                hooks=DurableRunHooks(),
                session=session,
                run_config=RunConfig(tracing_disabled=True),
            )
            stream_events = stream_result.stream_events()

            async for sdk_event in stream_events:
                # Persist lossless SDK data before projecting the public event.
                await runtime_context.record_sdk_event(sdk_event)
                event = translator.translate(sdk_event)
                if event is None:
                    continue

                produced_output = True
                if event.kind == "text_delta":
                    produced_text = True
                elif event.kind == "tool_call_end":
                    event = replace(
                        event,
                        output=normalize_tool_output(event.tool_name, event.output),
                    )
                await emit(event)

            if stream_result.run_loop_exception is not None:
                raise stream_result.run_loop_exception

            final_output = getattr(stream_result, "final_output", None)
            if not produced_text and final_output not in (None, ""):
                produced_output = True
                await emit(AgentEvent(kind="text_delta", value=str(final_output)))

            if not produced_output:
                raise RuntimeError("Agent run ended without producing any output.")

            await recorder.finish_turn(
                "complete",
                {"runId": request.run_id, "turnId": turn_id},
            )
            await emit(AgentEvent(kind="completed"))
        except asyncio.CancelledError:
            await _stop_stream(
                stream_result,
                stream_events,
                runtime_context.record_sdk_event
                if runtime_context is not None
                else None,
            )
            consumer_error = stream_state.consumer_error
            terminal_status = "error" if consumer_error is not None else "cancelled"
            terminal_payload: dict[str, object] = {
                "runId": request.run_id,
                "turnId": turn_id,
            }
            if consumer_error is not None:
                terminal_payload["error"] = str(consumer_error)

            if recorder is not None:
                with suppress(BaseException):
                    await recorder.finish_turn(
                        terminal_status,
                        terminal_payload,
                    )
            elif store is not None and turn_id is not None:
                with suppress(BaseException):
                    await asyncio.to_thread(
                        store.set_turn_status,
                        turn_id,
                        terminal_status,
                    )
            raise
        except Exception as exc:
            await _stop_stream(
                stream_result,
                stream_events,
                runtime_context.record_sdk_event
                if runtime_context is not None
                else None,
            )
            if recorder is not None:
                with suppress(BaseException):
                    await recorder.finish_turn(
                        "error",
                        {
                            "runId": request.run_id,
                            "turnId": turn_id,
                            "error": str(exc),
                        },
                    )
            elif store is not None and turn_id is not None:
                with suppress(BaseException):
                    await asyncio.to_thread(store.set_turn_status, turn_id, "error")
            await emit(AgentEvent(kind="error", error=str(exc)))
        finally:
            if context_tokens is not None:
                pop_chat_runtime_context(context_tokens)


def run_agent_to_completion(
    input: object,
    *,
    chat_id: str | None = None,
) -> str:
    """Run a non-streamed background invocation behind the runtime boundary."""
    response = Runner.run_sync(
        create_agent(chat_id=chat_id),
        input,
        max_turns=None,
        run_config=RunConfig(tracing_disabled=True),
    )
    return str(response.final_output or "")


__all__ = ["AgentRunRequest", "AgentRuntime", "run_agent_to_completion"]
