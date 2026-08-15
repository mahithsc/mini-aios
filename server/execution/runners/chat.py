from __future__ import annotations

import ast
import asyncio
import json
from contextlib import aclosing

from agno.agent import RunEvent as AgentRunEvent

from aios_core.agent import create_agent
from aios_core.runtime_context import (
    pop_chat_runtime_context,
    push_chat_runtime_context,
)
from aios_core.sessions import load_chat_session
from server.execution.service import RunsService, build_run_event
from server.lights import lights
from server.types.run import Run
from server.utils.utils import format_chat_messages_to_model_messages


AGENT_STREAM_IDLE_COMPLETION_SECONDS = 3.0


def _normalize_tool_result(tool_name: str, result: object) -> object:
    if tool_name not in {"show_canvas", "generative_widget"} or not isinstance(result, str):
        return result

    try:
        return json.loads(result)
    except json.JSONDecodeError:
        pass

    try:
        return ast.literal_eval(result)
    except (SyntaxError, ValueError):
        return result


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
        runtime_context_tokens = push_chat_runtime_context(chat_id)

        try:
            await lights.set_mode("thinking")
            agent = create_agent(chat_id=chat_id)
            async with aclosing(
                agent.arun(messages, stream=True, stream_events=True)
            ) as event_stream:
                completion_deadline: float | None = None
                active_tool_calls = 0
                event_iterator = aiter(event_stream)
                loop = asyncio.get_running_loop()

                while True:
                    timeout = (
                        max(0.0, completion_deadline - loop.time())
                        if completion_deadline is not None
                        and active_tool_calls == 0
                        else None
                    )

                    try:
                        event = await asyncio.wait_for(
                            anext(event_iterator),
                            timeout=timeout,
                        )
                    except StopAsyncIteration:
                        break
                    except TimeoutError:
                        # The configured OpenAI-compatible gateway can leave its
                        # SSE response open after the final text chunk. Once we
                        # have assistant content and no tool is active, an idle
                        # stream is a completed response rather than a running
                        # turn.
                        break

                    if (
                        event.event == AgentRunEvent.run_content
                        and event.content is not None
                        and event.content != ""
                    ):
                        produced_output = True
                        completion_deadline = (
                            loop.time()
                            + AGENT_STREAM_IDLE_COMPLETION_SECONDS
                        )
                        await runs_service.emit_event(
                            run.id,
                            build_run_event(
                                run_id=run.id,
                                event_type="token",
                                chat_id=chat_id,
                                data={"value": event.content},
                            ),
                        )
                    elif event.event == AgentRunEvent.run_content_completed:
                        # Agno performs post-response cleanup before yielding
                        # RunCompleted. A stalled cleanup must not leave a fully
                        # rendered reply (and its chat) stuck in "streaming".
                        break
                    elif event.event == AgentRunEvent.run_error:
                        await runs_service.emit_event(
                            run.id,
                            build_run_event(
                                run_id=run.id,
                                event_type="error",
                                chat_id=chat_id,
                                data={
                                    "error": event.content
                                    or "Agent run failed."
                                },
                            ),
                        )
                        return
                    elif event.event == AgentRunEvent.tool_call_started:
                        produced_output = True
                        completion_deadline = None
                        active_tool_calls += 1
                        tool = event.tool
                        await runs_service.emit_event(
                            run.id,
                            build_run_event(
                                run_id=run.id,
                                event_type="tool_call_start",
                                chat_id=chat_id,
                                data={
                                    "toolCallId": str(
                                        getattr(tool, "tool_call_id", None)
                                        or id(tool)
                                    ),
                                    "toolName": tool.tool_name,
                                    "input": tool.tool_args,
                                },
                            ),
                        )
                    elif event.event == AgentRunEvent.tool_call_completed:
                        produced_output = True
                        active_tool_calls = max(0, active_tool_calls - 1)
                        tool = event.tool
                        normalized_result = _normalize_tool_result(
                            tool.tool_name,
                            tool.result,
                        )
                        await runs_service.emit_event(
                            run.id,
                            build_run_event(
                                run_id=run.id,
                                event_type="tool_call_end",
                                chat_id=chat_id,
                                data={
                                    "toolCallId": str(
                                        getattr(tool, "tool_call_id", None)
                                        or id(tool)
                                    ),
                                    "toolName": tool.tool_name,
                                    "output": normalized_result,
                                },
                            ),
                        )
                    elif event.event == AgentRunEvent.custom_event:
                        if getattr(event, "kind", None) != "subagent_tool_event":
                            continue

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
                                        event,
                                        "parent_tool_call_id",
                                        None,
                                    ),
                                    "childRunId": getattr(
                                        event,
                                        "child_run_id",
                                        None,
                                    ),
                                    "childEventType": getattr(
                                        event,
                                        "child_event_type",
                                        None,
                                    ),
                                    "toolCallId": getattr(
                                        event,
                                        "tool_call_id",
                                        None,
                                    ),
                                    "toolName": tool_name,
                                    "input": getattr(event, "input", None),
                                    "output": _normalize_tool_result(
                                        (
                                            tool_name
                                            if isinstance(tool_name, str)
                                            else ""
                                        ),
                                        getattr(event, "output", None),
                                    ),
                                    "error": getattr(event, "error", None),
                                },
                            ),
                        )
        except Exception as exc:
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
                    data={
                        "error": "Agent run ended without producing any output."
                    },
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
