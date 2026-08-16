from __future__ import annotations

import ast
import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from agents import RunConfig, Runner
from agents.result import RunResultStreaming
from agents.stream_events import StreamEvent

from aios_core.agent import create_agent
from aios_core.openai_runtime import AgentRuntimeContext, OpenAIEventTranslator
from aios_core.runtime_context import (
    pop_chat_runtime_context,
    push_chat_runtime_context,
)
from aios_core.sessions import load_chat_session
from server.execution.service import RunsService, build_run_event
from server.lights import lights
from server.types.run import Run
from server.utils.utils import format_chat_messages_to_model_messages


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
) -> None:
    """Stop the SDK's background run and finish closing its event iterator."""
    if result is not None and not result.is_complete:
        result.cancel(mode="immediate")

    if events is None:
        return

    try:
        async for _ in events:
            pass
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

        messages = format_chat_messages_to_model_messages(load_chat_session(chat_id))
        await runs_service.emit_event(
            run.id,
            build_run_event(
                run_id=run.id,
                event_type="started",
                chat_id=chat_id,
            ),
        )

        produced_output = False
        produced_text = False
        runtime_context_tokens = push_chat_runtime_context(chat_id)
        stream_result: RunResultStreaming | None = None
        stream_events: AsyncIterator[StreamEvent] | None = None

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

        runtime_context = AgentRuntimeContext(event_sink=emit_nested_event)
        runtime_context.bind_to_current_loop()
        translator = OpenAIEventTranslator()

        try:
            await lights.set_mode("thinking")
            agent = create_agent(chat_id=chat_id)
            stream_result = Runner.run_streamed(
                agent,
                input=messages,
                context=runtime_context,
                max_turns=None,
                run_config=RunConfig(tracing_disabled=True),
            )
            stream_events = stream_result.stream_events()

            async for sdk_event in stream_events:
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
            await _stop_stream(stream_result, stream_events)
            raise
        except Exception as exc:
            await _stop_stream(stream_result, stream_events)
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
            pop_chat_runtime_context(runtime_context_tokens)
            await lights.set_mode("idle")

        if not produced_output:
            await runs_service.emit_event(
                run.id,
                build_run_event(
                    run_id=run.id,
                    event_type="error",
                    chat_id=chat_id,
                    data={"error": "Agent run ended without producing any output."},
                ),
            )
            return

        await runs_service.emit_event(
            run.id,
            build_run_event(
                run_id=run.id,
                event_type="completed",
                chat_id=chat_id,
            ),
        )
