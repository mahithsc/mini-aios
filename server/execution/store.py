from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from aios_core.sessions import load_chat_session, save_chat_session, update_chat_status
from aios_core.initialize import RUNS_EVENTS_DIR, RUNS_METADATA_DIR, RUNS_SNAPSHOTS_DIR
from pydantic import TypeAdapter

from server.types.chat import AssistantMessage, ChatMessage, LLMEvent, MessageStatus
from server.types.run import Run, RunCreateRequest, RunEvent, RunSnapshot, RunStatus

LLM_EVENT_ADAPTER = TypeAdapter(LLMEvent)


class RunStore:
    def create_run(self, request: RunCreateRequest) -> Run:
        raise NotImplementedError

    def get_run(self, run_id: str) -> Run | None:
        raise NotImplementedError

    def save_run(self, run: Run) -> Run:
        raise NotImplementedError

    def append_event(self, run_id: str, event: RunEvent) -> RunEvent:
        raise NotImplementedError

    def save_snapshot(
        self,
        run_id: str,
        *,
        status: RunStatus,
        last_sequence: int,
        preview: str | None = None,
        active_step: str | None = None,
    ) -> RunSnapshot:
        raise NotImplementedError

    def get_snapshot(self, run_id: str) -> RunSnapshot | None:
        raise NotImplementedError

    def list_snapshots(self, statuses: list[RunStatus] | None = None) -> list[RunSnapshot]:
        raise NotImplementedError

    def list_events_after(self, run_id: str, sequence: int) -> list[RunEvent]:
        raise NotImplementedError

    def project_chat_state(self, run_id: str, chat_id: str, event: RunEvent) -> None:
        raise NotImplementedError


class FileRunStore(RunStore):
    def __init__(
        self,
        metadata_dir: str | Path = RUNS_METADATA_DIR,
        snapshots_dir: str | Path = RUNS_SNAPSHOTS_DIR,
        events_dir: str | Path = RUNS_EVENTS_DIR,
    ) -> None:
        self._metadata_dir = Path(metadata_dir)
        self._snapshots_dir = Path(snapshots_dir)
        self._events_dir = Path(events_dir)
        self._ensure_directories()

    def create_run(self, request: RunCreateRequest) -> Run:
        now = int(time.time() * 1000)
        run = Run(
            id=str(uuid.uuid4()),
            kind=request.kind,
            status="queued",
            createdAt=now,
            updatedAt=now,
            chatId=request.chatId,
            sourceId=request.sourceId,
            turnId=request.turnId,
        )
        self.save_run(run)
        return run

    def get_run(self, run_id: str) -> Run | None:
        path = self._metadata_path(run_id)
        if not path.exists():
            return None
        return Run.model_validate(json.loads(path.read_text(encoding="utf-8")))

    def save_run(self, run: Run) -> Run:
        self._metadata_path(run.id).write_text(
            json.dumps(run.model_dump(mode="json"), indent=2),
            encoding="utf-8",
        )
        return run

    def append_event(self, run_id: str, event: RunEvent) -> RunEvent:
        self._ensure_run_exists(run_id)
        next_sequence = self._get_last_sequence(run_id) + 1
        persisted_event = event.model_copy(update={"sequence": next_sequence})
        with self._events_path(run_id).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(persisted_event.model_dump(mode="json")))
            handle.write("\n")
        return persisted_event

    def save_snapshot(
        self,
        run_id: str,
        *,
        status: RunStatus,
        last_sequence: int,
        preview: str | None = None,
        active_step: str | None = None,
    ) -> RunSnapshot:
        run = self._ensure_run_exists(run_id)
        updated_at = int(time.time() * 1000)
        snapshot = RunSnapshot(
            runId=run.id,
            kind=run.kind,
            status=status,
            updatedAt=updated_at,
            chatId=run.chatId,
            lastSequence=last_sequence,
            preview=preview,
            activeStep=active_step,
        )
        self._snapshot_path(run_id).write_text(
            json.dumps(snapshot.model_dump(mode="json"), indent=2),
            encoding="utf-8",
        )
        self.save_run(run.model_copy(update={"status": status, "updatedAt": updated_at}))
        return snapshot

    def get_snapshot(self, run_id: str) -> RunSnapshot | None:
        path = self._snapshot_path(run_id)
        if not path.exists():
            return None
        return RunSnapshot.model_validate(json.loads(path.read_text(encoding="utf-8")))

    def list_snapshots(self, statuses: list[RunStatus] | None = None) -> list[RunSnapshot]:
        allowed_statuses = set(statuses or [])
        snapshots: list[RunSnapshot] = []

        for path in self._snapshots_dir.glob("*.json"):
            snapshot = RunSnapshot.model_validate(json.loads(path.read_text(encoding="utf-8")))
            if allowed_statuses and snapshot.status not in allowed_statuses:
                continue
            snapshots.append(snapshot)

        snapshots.sort(key=lambda item: item.updatedAt, reverse=True)
        return snapshots

    def list_events_after(self, run_id: str, sequence: int) -> list[RunEvent]:
        path = self._events_path(run_id)
        if not path.exists():
            return []

        events: list[RunEvent] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = RunEvent.model_validate(json.loads(line))
            if event.sequence > sequence:
                events.append(event)
        return events

    def project_chat_state(self, run_id: str, chat_id: str, event: RunEvent) -> None:
        llm_event = _run_event_to_chat_event(event)
        if llm_event is None:
            return

        messages = load_chat_session(chat_id)
        save_chat_session(chat_id, _apply_chat_event(messages, run_id, llm_event))
        update_chat_status(chat_id, _chat_status_for_event(LLM_EVENT_ADAPTER.validate_python(llm_event)))

    def _ensure_directories(self) -> None:
        self._metadata_dir.mkdir(parents=True, exist_ok=True)
        self._snapshots_dir.mkdir(parents=True, exist_ok=True)
        self._events_dir.mkdir(parents=True, exist_ok=True)

    def _metadata_path(self, run_id: str) -> Path:
        return self._metadata_dir / f"{run_id}.json"

    def _snapshot_path(self, run_id: str) -> Path:
        return self._snapshots_dir / f"{run_id}.json"

    def _events_path(self, run_id: str) -> Path:
        return self._events_dir / f"{run_id}.jsonl"

    def _ensure_run_exists(self, run_id: str) -> Run:
        run = self.get_run(run_id)
        if run is None:
            raise KeyError(f"Run {run_id} does not exist.")
        return run

    def _get_last_sequence(self, run_id: str) -> int:
        snapshot = self.get_snapshot(run_id)
        if snapshot is not None:
            return snapshot.lastSequence

        events = self.list_events_after(run_id, 0)
        if not events:
            return 0
        return events[-1].sequence


def _run_event_to_chat_event(event: RunEvent) -> dict[str, object] | None:
    data = event.event.data or {}
    payload = {
        "id": f"{event.runId}:{event.sequence}",
        "createdAt": event.createdAt,
    }

    if event.event.type == "started":
        return {
            **payload,
            "type": "stream_start",
        }

    if event.event.type == "token":
        value = data.get("value")
        if isinstance(value, str):
            return {
                **payload,
                "type": "token",
                "value": value,
            }
        return None

    if event.event.type == "tool_call_start":
        tool_call_id = data.get("toolCallId")
        tool_name = data.get("toolName")
        if isinstance(tool_call_id, str) and isinstance(tool_name, str):
            return {
                **payload,
                "type": "tool_call_start",
                "toolCallId": tool_call_id,
                "toolName": tool_name,
                "input": data.get("input"),
            }
        return None

    if event.event.type == "tool_call_end":
        tool_call_id = data.get("toolCallId")
        tool_name = data.get("toolName")
        if isinstance(tool_call_id, str) and isinstance(tool_name, str):
            return {
                **payload,
                "type": "tool_call_end",
                "toolCallId": tool_call_id,
                "toolName": tool_name,
                "output": data.get("output"),
            }
        return None

    if event.event.type == "error":
        error = data.get("error")
        return {
            **payload,
            "type": "stream_error",
            "error": error if isinstance(error, str) else "Run failed.",
        }

    if event.event.type == "cancelled":
        reason = data.get("reason")
        return {
            **payload,
            "type": "stream_cancelled",
            "reason": reason if isinstance(reason, str) else "Run stopped by user.",
        }

    if event.event.type == "completed":
        return {
            **payload,
            "type": "stream_end",
        }

    if event.event.type == "subagent_tool_event":
        parent_tool_call_id = data.get("parentToolCallId")
        child_run_id = data.get("childRunId")
        child_event_type = data.get("childEventType")
        tool_call_id = data.get("toolCallId")
        tool_name = data.get("toolName")
        error = data.get("error")

        if (
            not isinstance(parent_tool_call_id, str)
            or not isinstance(child_run_id, str)
            or child_event_type
            not in {
                "stream_start",
                "tool_call_start",
                "tool_call_end",
                "tool_call_error",
                "stream_end",
                "stream_error",
            }
        ):
            return None

        return {
            **payload,
            "type": "subagent_tool_event",
            "parentToolCallId": parent_tool_call_id,
            "childRunId": child_run_id,
            "childEventType": child_event_type,
            "toolCallId": tool_call_id if isinstance(tool_call_id, str) else None,
            "toolName": tool_name if isinstance(tool_name, str) else None,
            "input": data.get("input"),
            "output": data.get("output"),
            "error": error if isinstance(error, str) else None,
        }

    return None


def _apply_chat_event(messages: list[ChatMessage], run_id: str, event: dict[str, object]) -> list[ChatMessage]:
    parsed_event = LLM_EVENT_ADAPTER.validate_python(event)

    assistant_index = next(
        (
            index
            for index in range(len(messages) - 1, -1, -1)
            if isinstance(messages[index], AssistantMessage) and messages[index].runId == run_id
        ),
        -1,
    )

    if assistant_index == -1:
        return [*messages, _create_assistant_message(run_id, parsed_event)]

    next_messages = list(messages)
    assistant_message = next_messages[assistant_index]
    if not isinstance(assistant_message, AssistantMessage):
        return messages

    next_messages[assistant_index] = assistant_message.model_copy(
        update={
            "updatedAt": parsed_event.createdAt,
            "status": _assistant_status_for_event(parsed_event),
            "events": _append_assistant_events(list(assistant_message.events), parsed_event),
        }
    )
    return next_messages


def _create_assistant_message(run_id: str, event: LLMEvent) -> AssistantMessage:
    created_at = event.createdAt
    return AssistantMessage(
        id=str(uuid.uuid4()),
        createdAt=created_at,
        updatedAt=created_at,
        status=_assistant_status_for_event(event),
        role="assistant",
        runId=run_id,
        events=[event],
    )


def _append_assistant_events(events: list[LLMEvent], event: LLMEvent) -> list[LLMEvent]:
    if event.type == "token" and events and events[-1].type == "token":
        previous_event = events[-1]
        events[-1] = previous_event.model_copy(update={"value": previous_event.value + event.value})
        return events

    events.append(event)
    return events


def _assistant_status_for_event(event: LLMEvent) -> MessageStatus:
    event_type = event.type
    if event_type == "stream_error":
        return "error"
    if event_type == "stream_cancelled":
        return "cancelled"
    if event_type == "stream_end":
        return "complete"
    return "streaming"


def _chat_status_for_event(event: LLMEvent) -> str:
    if event.type == "stream_error":
        return "error"
    if event.type == "stream_cancelled":
        return "cancelled"
    if event.type == "stream_end":
        return "idle"
    return "streaming"
