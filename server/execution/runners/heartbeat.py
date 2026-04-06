from __future__ import annotations

import ast
import json
import logging
from datetime import datetime, timezone

from agno.agent import RunEvent as AgentRunEvent

from aios_core.agent import create_agent
from aios_core.prompt_loader import load_prompt
from aios_core.workspace import ensure_workspace_dir
from server.execution.service import RunsService, build_run_event
from server.types.run import Run

log = logging.getLogger(__name__)
_HEARTBEAT_LOG_DIR = ensure_workspace_dir() / "heartbeat_logs"
_HEARTBEAT_PROMPT = load_prompt("heartbeat.md")


def _normalize_tool_result(tool_name: str, result: object) -> object:
    if tool_name != "show_canvas" or not isinstance(result, str):
        return result

    try:
        return json.loads(result)
    except json.JSONDecodeError:
        pass

    try:
        return ast.literal_eval(result)
    except (SyntaxError, ValueError):
        return result


def _write_heartbeat_log(
    *,
    run_id: str,
    started: str,
    finished: str,
    status: str,
    output: str,
) -> None:
    _HEARTBEAT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = _HEARTBEAT_LOG_DIR / f"{started.replace(':', '-')}_{run_id}.log"
    log_path.write_text(
        f"run_id: {run_id}\nstarted: {started}\nfinished: {finished}\nstatus: {status}\n\n{output}",
        encoding="utf-8",
    )


class HeartbeatRunner:
    kind = "heartbeat"

    async def execute(self, run: Run, runs_service: RunsService) -> None:
        started = datetime.now(timezone.utc).isoformat()
        status = "completed"
        output_chunks: list[str] = []
        received_text = False

        try:
            await runs_service.emit_event(
                run.id,
                build_run_event(
                    run_id=run.id,
                    event_type="started",
                ),
            )

            agent = create_agent()
            messages = [{"role": "user", "content": _HEARTBEAT_PROMPT}]
            async for event in agent.arun(messages, stream=True, stream_events=True):
                if event.event == AgentRunEvent.run_content and event.content is not None:
                    text = str(event.content)
                    if text:
                        received_text = True
                        output_chunks.append(text)
                        await runs_service.emit_event(
                            run.id,
                            build_run_event(
                                run_id=run.id,
                                event_type="token",
                                data={"value": text},
                            ),
                        )
                elif event.event == AgentRunEvent.run_completed and event.content is not None:
                    text = str(event.content)
                    if text and not received_text:
                        received_text = True
                        output_chunks.append(text)
                        await runs_service.emit_event(
                            run.id,
                            build_run_event(
                                run_id=run.id,
                                event_type="token",
                                data={"value": text},
                            ),
                        )
                elif event.event == AgentRunEvent.run_error:
                    status = "error"
                    error = str(event.content or "Heartbeat run failed.")
                    output_chunks.append(error)
                    await runs_service.emit_event(
                        run.id,
                        build_run_event(
                            run_id=run.id,
                            event_type="error",
                            data={"error": error},
                        ),
                    )
                    return
                elif event.event == AgentRunEvent.tool_call_started:
                    tool = event.tool
                    await runs_service.emit_event(
                        run.id,
                        build_run_event(
                            run_id=run.id,
                            event_type="tool_call_start",
                            data={
                                "toolCallId": str(getattr(tool, "tool_call_id", None) or id(tool)),
                                "toolName": tool.tool_name,
                                "input": tool.tool_args,
                            },
                        ),
                    )
                elif event.event == AgentRunEvent.tool_call_completed:
                    tool = event.tool
                    await runs_service.emit_event(
                        run.id,
                        build_run_event(
                            run_id=run.id,
                            event_type="tool_call_end",
                            data={
                                "toolCallId": str(getattr(tool, "tool_call_id", None) or id(tool)),
                                "toolName": tool.tool_name,
                                "output": _normalize_tool_result(tool.tool_name, tool.result),
                            },
                        ),
                    )
                elif event.event == AgentRunEvent.custom_event:
                    if getattr(event, "kind", None) != "subagent_tool_event":
                        continue

                    tool_name = getattr(event, "tool_name", None)
                    await runs_service.emit_event(
                        run.id,
                        build_run_event(
                            run_id=run.id,
                            event_type="subagent_tool_event",
                            data={
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

            if not received_text:
                status = "error"
                error = "Heartbeat run ended without producing a status note."
                output_chunks.append(error)
                await runs_service.emit_event(
                    run.id,
                    build_run_event(
                        run_id=run.id,
                        event_type="error",
                        data={"error": error},
                    ),
                )
                return

            await runs_service.emit_event(
                run.id,
                build_run_event(
                    run_id=run.id,
                    event_type="completed",
                ),
            )
        except Exception as exc:
            status = "error"
            output_chunks.append(str(exc))
            log.error("Heartbeat run %s failed: %s", run.id, exc)
            await runs_service.emit_event(
                run.id,
                build_run_event(
                    run_id=run.id,
                    event_type="error",
                    data={"error": str(exc)},
                ),
            )
            return
        finally:
            finished = datetime.now(timezone.utc).isoformat()
            _write_heartbeat_log(
                run_id=run.id,
                started=started,
                finished=finished,
                status=status,
                output="".join(output_chunks).strip(),
            )
