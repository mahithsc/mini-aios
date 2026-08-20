from __future__ import annotations

import ast
import json

from agno.agent import RunEvent as AgentRunEvent
from agno.models.message import Message

from aios_core.agent import create_agent
from aios_core.deploy.disclosures import (
    missing_disclosure_suffix,
    required_disclosures_from_tool_result,
)
from aios_core.runtime_context import (
    pop_chat_runtime_context,
    push_chat_runtime_context,
)
from aios_core.sessions import load_chat_session
from aios_core.tools.codex_job import _manager as codex_job_manager
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


def _codex_context_messages(run: Run) -> list[Message]:
    records: list[dict] = []
    if run.sourceId and run.sourceId.startswith("codex:"):
        job_id = run.sourceId.split(":", 1)[1]
        record = codex_job_manager.store.get(job_id)
        if record is not None:
            records.append(record)
    elif run.chatId:
        records.extend(
            record
            for record in codex_job_manager.list_for_session(run.chatId)
            if record.get("status") == "awaiting_input"
        )

    messages: list[Message] = []
    for record in records[:3]:
        status = str(record.get("status") or run.turnId or "unknown")
        common = (
            "This is trusted runtime context for a Codex child run. "
            f"Job id: {record.get('job_id')}. Status: {status}. "
            f"Delegated task: {record.get('task')}. "
            f"Working directory: {record.get('workdir')}. "
        )
        if status == "done":
            workspace_handoff = record.get("workspace_handoff")
            handoff_context = ""
            if isinstance(workspace_handoff, dict):
                handoff_context = (
                    "Trusted completed workspace_handoff: "
                    f"{json.dumps(workspace_handoff, default=str, sort_keys=True)}. "
                    "For deployment, call create_app_artifact with only its exact "
                    "handoff_id. "
                )
            instruction = (
                f"Codex reported: {record.get('result') or '(empty)'}. "
                f"{handoff_context}"
                "Inspect the resulting files, run proportionate verification, and "
                "then give the user a concise verified final response. Do not merely "
                "repeat Codex's claim."
            )
        elif status == "awaiting_input":
            instruction = (
                "Codex is waiting for user input. Ask the user the following "
                "questions faithfully and do not invent answers: "
                f"{json.dumps(record.get('pending_input'), default=str)}. "
                "If the latest user message already supplies the answers, call "
                "codex_answer with this job id and the matching question ids."
            )
        elif status == "error":
            instruction = (
                f"Codex failed: {record.get('error') or 'unknown error'}. "
                "Inspect any partial work if useful, then explain the failure and "
                "the safest next step to the user."
            )
        else:
            instruction = (
                "Use this state only as context for the user's latest request."
            )
        messages.append(Message(role="system", content=common + instruction))
    return messages


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
        messages.extend(_codex_context_messages(run))
        await runs_service.emit_event(
            run.id,
            build_run_event(
                run_id=run.id,
                event_type="started",
                chat_id=chat_id,
            ),
        )

        produced_output = False
        output_text: list[str] = []
        required_disclosures: list[str] = []
        runtime_context_tokens = push_chat_runtime_context(chat_id, run.id)

        try:
            await lights.set_mode("thinking")
            agent = create_agent(chat_id=chat_id)
            async for event in agent.arun(messages, stream=True, stream_events=True):
                if (
                    event.event == AgentRunEvent.run_content
                    and event.content is not None
                ):
                    produced_output = True
                    output_text.append(str(event.content))
                    await runs_service.emit_event(
                        run.id,
                        build_run_event(
                            run_id=run.id,
                            event_type="token",
                            chat_id=chat_id,
                            data={"value": event.content},
                        ),
                    )
                elif event.event == AgentRunEvent.run_error:
                    await runs_service.emit_event(
                        run.id,
                        build_run_event(
                            run_id=run.id,
                            event_type="error",
                            chat_id=chat_id,
                            data={"error": event.content or "Agent run failed."},
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
                            chat_id=chat_id,
                            data={
                                "toolCallId": str(
                                    getattr(tool, "tool_call_id", None) or id(tool)
                                ),
                                "toolName": tool.tool_name,
                                "input": tool.tool_args,
                            },
                        ),
                    )
                elif event.event == AgentRunEvent.tool_call_completed:
                    produced_output = True
                    tool = event.tool
                    normalized_result = _normalize_tool_result(
                        tool.tool_name, tool.result
                    )
                    for disclosure in required_disclosures_from_tool_result(
                        normalized_result
                    ):
                        if disclosure not in required_disclosures:
                            required_disclosures.append(disclosure)
                    await runs_service.emit_event(
                        run.id,
                        build_run_event(
                            run_id=run.id,
                            event_type="tool_call_end",
                            chat_id=chat_id,
                            data={
                                "toolCallId": str(
                                    getattr(tool, "tool_call_id", None) or id(tool)
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
                                    event, "parent_tool_call_id", None
                                ),
                                "childRunId": getattr(event, "child_run_id", None),
                                "childEventType": getattr(
                                    event, "child_event_type", None
                                ),
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

        disclosure_suffix = missing_disclosure_suffix(
            "".join(output_text), required_disclosures
        )
        if disclosure_suffix:
            await runs_service.emit_event(
                run.id,
                build_run_event(
                    run_id=run.id,
                    event_type="token",
                    chat_id=chat_id,
                    data={"value": disclosure_suffix},
                ),
            )

        await runs_service.emit_event(
            run.id,
            build_run_event(
                run_id=run.id,
                event_type="completed",
                chat_id=chat_id,
            ),
        )
