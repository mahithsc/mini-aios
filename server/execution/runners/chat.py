from __future__ import annotations

import ast
import asyncio
import json
import weakref
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from typing import Any

from agents import RunConfig, Runner
from agents.result import RunResultStreaming
from agents.stream_events import StreamEvent

from aios_core.agent import create_agent
from aios_core.conversation_store import (
    CanonicalConversationSession,
    ConversationRecorder,
    ConversationStore,
    DurableRunHooks,
    MAIN_SCOPE,
)
from aios_core.openai_runtime import AgentRuntimeContext, OpenAIEventTranslator
from aios_core.runtime_context import (
    pop_chat_runtime_context,
    push_chat_runtime_context,
)
from aios_core.sessions import load_chat_session
from server.execution.service import RunsService, build_run_event
from server.lights import lights
from server.types.chat import ChatMessage, UserMessage
from server.types.run import Run
from server.utils.utils import (
    chat_message_to_model_message,
    legacy_chat_message_to_model_item,
)


_CHAT_LOCKS: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[str, asyncio.Lock]
] = weakref.WeakKeyDictionary()


def _chat_lock(chat_id: str) -> asyncio.Lock:
    """Return a lock bound to this event loop and chat.

    The run service can have multiple workers.  Serializing the whole
    load/model/save interval prevents two turns in one chat from branching
    from the same canonical history.  Locks are loop-scoped so test suites and
    embedded runtimes that create more than one event loop do not reuse a lock
    bound to a closed loop.
    """
    loop = asyncio.get_running_loop()
    locks = _CHAT_LOCKS.setdefault(loop, {})
    return locks.setdefault(chat_id, asyncio.Lock())


def _current_user_message(
    messages: list[ChatMessage],
    turn_id: str | None,
) -> tuple[int, UserMessage]:
    if turn_id:
        for index, message in enumerate(messages):
            if isinstance(message, UserMessage) and message.id == turn_id:
                return index, message
        raise RuntimeError(f"User message {turn_id} was not found in this chat.")

    # Compatibility for queued runs created before turnId was added. New runs
    # always carry the exact user-message UUID from the gateway.
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, UserMessage):
            return index, message
    raise RuntimeError("Chat run has no user message to process.")


def _normalize_tool_result(tool_name: str, result: object) -> object:
    if tool_name not in {"show_canvas", "generative_widget"} or not isinstance(
        result, str
    ):
        return result

    try:
        return json.loads(result)
    except json.JSONDecodeError:
        pass

    try:
        return ast.literal_eval(result)
    except (SyntaxError, ValueError):
        return result


async def _stop_stream(
    result: RunResultStreaming | None,
    events: AsyncIterator[StreamEvent] | None,
    record_event: Callable[[StreamEvent], Awaitable[object]] | None = None,
) -> None:
    """Stop the SDK's background run and finish closing its event iterator."""
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
        # Cleanup must not replace the original cancellation or run error.
        pass
    finally:
        try:
            await events.aclose()
        except BaseException:
            pass


class ChatRunner:
    kind = "chat"

    async def execute(self, run: Run, runs_service: RunsService) -> None:
        chat_id = run.chatId
        if not chat_id:
            await runs_service.emit_event(
                run.id,
                build_run_event(
                    run_id=run.id,
                    event_type="error",
                    chat_id=None,
                    data={"error": "Chat run is missing chatId."},
                ),
            )
            return

        async with _chat_lock(chat_id):
            await self._execute_serialized(run, runs_service, chat_id)

    async def _execute_serialized(
        self,
        run: Run,
        runs_service: RunsService,
        chat_id: str,
    ) -> None:
        produced_output = False
        produced_text = False
        runtime_context_tokens: tuple[Any, ...] | None = None
        thinking = False
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
            await runs_service.emit_event(
                run.id,
                build_run_event(
                    run_id=run.id,
                    event_type="subagent_tool_event",
                    chat_id=chat_id,
                    data={
                        "parentToolCallId": getattr(
                            event, "parent_tool_call_id", None
                        ),
                        "childRunId": getattr(event, "child_run_id", None),
                        "childEventType": getattr(event, "child_event_type", None),
                        "toolCallId": getattr(event, "tool_call_id", None),
                        "toolName": tool_name,
                        "input": getattr(event, "input", None),
                        "output": _normalize_tool_result(
                            tool_name if isinstance(tool_name, str) else "",
                            getattr(event, "output", None),
                        ),
                        "error": getattr(event, "error", None),
                    },
                ),
            )

        try:
            transcript = load_chat_session(chat_id)
            current_index, current_user = _current_user_message(
                transcript,
                run.turnId,
            )
            turn_id = run.turnId or current_user.id
            current_input = chat_message_to_model_message(current_user)
            legacy_seed: list[tuple[str, Any]] = []
            for message in transcript[:current_index]:
                seed_item = legacy_chat_message_to_model_item(message)
                if seed_item is not None:
                    legacy_seed.append((message.id, seed_item))

            store = ConversationStore()
            await asyncio.to_thread(
                store.create_turn,
                chat_id=chat_id,
                turn_id=turn_id,
                user_message_id=current_user.id,
                run_id=run.id,
            )
            await asyncio.to_thread(store.ensure_seeded, chat_id, legacy_seed)
            # Reconcile any user rows committed while an earlier run was
            # queued/cancelled and therefore never reached its own preflight.
            # Native assistant/tool items are never reconstructed from the UI.
            for message in transcript[:current_index]:
                if not isinstance(message, UserMessage):
                    continue
                await asyncio.to_thread(
                    store.append_items,
                    chat_id=chat_id,
                    scope_key=MAIN_SCOPE,
                    run_id=None,
                    turn_id=None,
                    items=[chat_message_to_model_message(message)],
                    source="ui_message",
                    replayable=True,
                    source_message_id=message.id,
                    dedupe_prefix="runner-reconcile",
                )
            await asyncio.to_thread(store.set_turn_status, turn_id, "running")
            recorder = ConversationRecorder(
                store=store,
                chat_id=chat_id,
                run_id=run.id,
                turn_id=turn_id,
            )
            await recorder.record_application_event(
                "run.started",
                {"runId": run.id, "turnId": turn_id},
            )

            session = CanonicalConversationSession(
                store=store,
                chat_id=chat_id,
                run_id=run.id,
                turn_id=turn_id,
                current_user_message_id=current_user.id,
                current_input=current_input,
            )
            # Persist the user item before entering the SDK. The SDK writes it
            # again immediately before the provider call; stable source-message
            # identity makes that write idempotent. This preserves user history
            # even when setup/provider failure happens before the SDK save point.
            await session.add_items([current_input])
            runtime_context = AgentRuntimeContext(
                event_sink=emit_nested_event,
                conversation_recorder=recorder,
            )
            runtime_context.bind_to_current_loop()
            translator = OpenAIEventTranslator()
            runtime_context_tokens = push_chat_runtime_context(chat_id)

            await runs_service.emit_event(
                run.id,
                build_run_event(
                    run_id=run.id,
                    event_type="started",
                    chat_id=chat_id,
                ),
            )
            await lights.set_mode("thinking")
            thinking = True
            agent = create_agent(chat_id=chat_id)
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
                # Persist the complete native stream event before translating
                # it into the smaller desktop event protocol. This captures
                # reasoning and function-argument deltas that the UI ignores.
                await runtime_context.record_sdk_event(sdk_event)
                event = translator.translate(sdk_event)
                if event is None:
                    continue

                if event.kind == "text":
                    produced_output = True
                    produced_text = True
                    await runs_service.emit_event(
                        run.id,
                        build_run_event(
                            run_id=run.id,
                            event_type="token",
                            chat_id=chat_id,
                            data={"value": event.value},
                        ),
                    )
                    continue

                if event.kind == "tool_start":
                    produced_output = True
                    await runs_service.emit_event(
                        run.id,
                        build_run_event(
                            run_id=run.id,
                            event_type="tool_call_start",
                            chat_id=chat_id,
                            data={
                                "toolCallId": event.tool_call_id,
                                "toolName": event.tool_name,
                                "input": event.input,
                            },
                        ),
                    )
                    continue

                if event.kind == "tool_end":
                    produced_output = True
                    await runs_service.emit_event(
                        run.id,
                        build_run_event(
                            run_id=run.id,
                            event_type="tool_call_end",
                            chat_id=chat_id,
                            data={
                                "toolCallId": event.tool_call_id,
                                "toolName": event.tool_name,
                                "output": _normalize_tool_result(
                                    event.tool_name,
                                    event.output,
                                ),
                            },
                        ),
                    )

            if stream_result.run_loop_exception is not None:
                raise stream_result.run_loop_exception

            final_output = getattr(stream_result, "final_output", None)
            if not produced_text and final_output not in (None, ""):
                produced_output = True
                await runs_service.emit_event(
                    run.id,
                    build_run_event(
                        run_id=run.id,
                        event_type="token",
                        chat_id=chat_id,
                        data={"value": str(final_output)},
                    ),
                )
        except asyncio.CancelledError:
            await _stop_stream(
                stream_result,
                stream_events,
                runtime_context.record_sdk_event if runtime_context is not None else None,
            )
            if recorder is not None:
                with suppress(BaseException):
                    await recorder.finish_turn(
                        "cancelled",
                        {"runId": run.id, "turnId": turn_id},
                    )
            elif store is not None and turn_id is not None:
                with suppress(BaseException):
                    await asyncio.to_thread(
                        store.set_turn_status,
                        turn_id,
                        "cancelled",
                    )
            raise
        except Exception as exc:
            await _stop_stream(
                stream_result,
                stream_events,
                runtime_context.record_sdk_event if runtime_context is not None else None,
            )
            if recorder is not None:
                with suppress(BaseException):
                    await recorder.finish_turn(
                        "error",
                        {"runId": run.id, "turnId": turn_id, "error": str(exc)},
                    )
            elif store is not None and turn_id is not None:
                with suppress(BaseException):
                    await asyncio.to_thread(store.set_turn_status, turn_id, "error")
            await runs_service.emit_event(
                run.id,
                build_run_event(
                    run_id=run.id,
                    event_type="error",
                    chat_id=chat_id,
                    data={"error": str(exc)},
                ),
            )
            return
        finally:
            if runtime_context_tokens is not None:
                pop_chat_runtime_context(runtime_context_tokens)
            if thinking:
                await lights.set_mode("idle")

        if not produced_output:
            error = "Agent run ended without producing any output."
            if recorder is not None:
                await recorder.finish_turn(
                    "error",
                    {"runId": run.id, "turnId": turn_id, "error": error},
                )
            elif store is not None and turn_id is not None:
                await asyncio.to_thread(store.set_turn_status, turn_id, "error")
            await runs_service.emit_event(
                run.id,
                build_run_event(
                    run_id=run.id,
                    event_type="error",
                    chat_id=chat_id,
                    data={"error": error},
                ),
            )
            return

        if recorder is not None:
            await recorder.finish_turn(
                "complete",
                {"runId": run.id, "turnId": turn_id},
            )
        elif store is not None and turn_id is not None:
            await asyncio.to_thread(store.set_turn_status, turn_id, "complete")
        await runs_service.emit_event(
            run.id,
            build_run_event(
                run_id=run.id,
                event_type="completed",
                chat_id=chat_id,
            ),
        )
