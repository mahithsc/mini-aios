from time import monotonic
from uuid import uuid4

from agno.run.agent import RunEvent as AgentRunEvent

from ..prompt_loader import render_prompt
from .subagent_events import (
    SubagentStreamEvent,
    build_subagent_stream_event as _build_subagent_stream_event,
)

__all__ = ["subagent", "SubagentStreamEvent"]


def subagent(task: str | None = None, timeout: float = 60, fc=None):
    """
    Run delegated subagent work and stream child tool events.
    The tool still blocks until the subagent finishes and returns its final text.
    """
    if timeout is None or float(timeout) <= 0:
        yield "error: timeout must be > 0"
        return

    if not isinstance(task, str) or not task.strip():
        yield "error: task is required"
        return

    from aios_core.agent import create_subagent_worker

    child_run_id = str(uuid4())
    parent_tool_call_id = str(getattr(fc, "call_id", None) or child_run_id)
    prompt = render_prompt("subagent.md", task=task.strip())
    agent = create_subagent_worker()
    output_chunks: list[str] = []
    received_text = False
    deadline = monotonic() + float(timeout)

    yield _build_subagent_stream_event(
        parent_tool_call_id=parent_tool_call_id,
        child_run_id=child_run_id,
        child_event_type="stream_start",
    )

    try:
        for event in agent.run(
            [{"role": "user", "content": prompt}],
            stream=True,
            stream_events=True,
            run_id=child_run_id,
        ):
            if monotonic() > deadline:
                raise TimeoutError

            if event.event == AgentRunEvent.run_content and event.content is not None:
                received_text = True
                output_chunks.append(str(event.content))
                continue

            if event.event == AgentRunEvent.tool_call_started:
                tool = event.tool
                if tool is None:
                    continue
                yield _build_subagent_stream_event(
                    parent_tool_call_id=parent_tool_call_id,
                    child_run_id=child_run_id,
                    child_event_type="tool_call_start",
                    tool_call_id=str(getattr(tool, "tool_call_id", None) or id(tool)),
                    tool_name=tool.tool_name,
                    input=tool.tool_args,
                )
                continue

            if event.event == AgentRunEvent.tool_call_completed:
                tool = event.tool
                if tool is None:
                    continue
                yield _build_subagent_stream_event(
                    parent_tool_call_id=parent_tool_call_id,
                    child_run_id=child_run_id,
                    child_event_type="tool_call_end",
                    tool_call_id=str(getattr(tool, "tool_call_id", None) or id(tool)),
                    tool_name=tool.tool_name,
                    output=tool.result,
                )
                continue

            if event.event == AgentRunEvent.tool_call_error:
                tool = event.tool
                yield _build_subagent_stream_event(
                    parent_tool_call_id=parent_tool_call_id,
                    child_run_id=child_run_id,
                    child_event_type="tool_call_error",
                    tool_call_id=(
                        str(getattr(tool, "tool_call_id", None) or id(tool))
                        if tool is not None
                        else None
                    ),
                    tool_name=tool.tool_name if tool is not None else None,
                    error=str(getattr(event, "error", None) or "Subagent tool call failed."),
                )
                continue

            if event.event == AgentRunEvent.run_error:
                error = str(event.content or "Subagent run failed.")
                yield _build_subagent_stream_event(
                    parent_tool_call_id=parent_tool_call_id,
                    child_run_id=child_run_id,
                    child_event_type="stream_error",
                    error=error,
                )
                yield f"error: subagent failed -- {error}"
                return

            if event.event == AgentRunEvent.run_completed and not received_text:
                if event.content is not None:
                    output_chunks.append(str(event.content))
                continue
    except TimeoutError:
        yield _build_subagent_stream_event(
            parent_tool_call_id=parent_tool_call_id,
            child_run_id=child_run_id,
            child_event_type="stream_error",
            error=f"Subagent timed out after {float(timeout):g}s.",
        )
        yield f"error: subagent timed out after {float(timeout):g}s"
        return
    except Exception as exc:
        yield _build_subagent_stream_event(
            parent_tool_call_id=parent_tool_call_id,
            child_run_id=child_run_id,
            child_event_type="stream_error",
            error=str(exc),
        )
        yield f"error: subagent failed -- {exc}"
        return

    yield _build_subagent_stream_event(
        parent_tool_call_id=parent_tool_call_id,
        child_run_id=child_run_id,
        child_event_type="stream_end",
    )
    yield "".join(output_chunks).strip() or "(empty)"
