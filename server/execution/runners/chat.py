from __future__ import annotations

import ast
import json

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
            async for event in agent.arun(messages, stream=True, stream_events=True):
                if event.event == AgentRunEvent.run_content and event.content is not None:
                    produced_output = True
                    await runs_service.emit_event(
                        run.id,
                        build_run_event(
                            run_id=run.id,
                            event_type="token",
                            chat_id=chat_id,                            data={"value": event.content},
                        ),
                    )
                elif event.event == AgentRunEvent.run_error:
                    await runs_service.emit_event(
                        run.id,
                        build_run_event(
                            run_id=run.id,
                            event_type="error",
                            chat_id=chat_id,                            data={"error": event.content or "Agent run failed."},
                        ),
                    )
                    return
                elif event.event == AgentRunEvent.tool_call_started:
                    produced_output = True
                    tool = event.tool
                    await runs_service.emit_event(
                        run.id,
                        build_run_event(
                            run_id=run.id,
                            event_type="tool_call_start",
                            chat_id=chat_id,                            data={
                                "toolCallId": str(getattr(tool, "tool_call_id", None) or id(tool)),
                                "toolName": tool.tool_name,
                                "input": tool.tool_args,
                            },
                        ),
                    )
                elif event.event == AgentRunEvent.tool_call_completed:
                    produced_output = True
                    tool = event.tool
                    normalized_result = _normalize_tool_result(tool.tool_name, tool.result)
                    await runs_service.emit_event(
                        run.id,
                        build_run_event(
                            run_id=run.id,
                            event_type="tool_call_end",
                            chat_id=chat_id,                            data={
                                "toolCallId": str(getattr(tool, "tool_call_id", None) or id(tool)),
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
                            chat_id=chat_id,                            data={
                                "parentToolCallId": getattr(event, "parent_tool_call_id", None),
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
        except Exception as exc:
            await runs_service.emit_event(
                run.id,
                build_run_event(
                    run_id=run.id,
                    event_type="error",
                    chat_id=chat_id,                    data={"error": str(exc)},
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
                    chat_id=chat_id,                    data={"error": "Agent run ended without producing any output."},
                ),
            )
            return

        await runs_service.emit_event(
            run.id,
            build_run_event(
                run_id=run.id,
                event_type="completed",
                chat_id=chat_id,            ),
        )
