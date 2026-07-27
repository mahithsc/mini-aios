from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path

from aios_core.initialize import RUNS_EVENTS_DIR, RUNS_METADATA_DIR, RUNS_SNAPSHOTS_DIR
from aios_core.sessions import append_assistant_event
from pydantic import ValidationError

from server.types.run import Run, RunCreateRequest, RunEvent, RunKind, RunSnapshot, RunStatus

log = logging.getLogger(__name__)


class RunStore:
    def create_run(self, request: RunCreateRequest, *, user_id: str | None = None) -> Run:
        raise NotImplementedError

    def get_run(self, run_id: str, *, user_id: str | None = None) -> Run | None:
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

    def list_snapshots(
        self,
        user_id: str | None = None,
        statuses: list[RunStatus] | None = None,
        kinds: list[RunKind] | None = None,
        limit: int | None = None,
    ) -> list[RunSnapshot]:
        raise NotImplementedError

    def list_events_after(
        self,
        run_id: str,
        sequence: int,
        *,
        user_id: str | None = None,
    ) -> list[RunEvent]:
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

    def create_run(self, request: RunCreateRequest, *, user_id: str | None = None) -> Run:
        now = int(time.time() * 1000)
        run = Run(
            id=str(uuid.uuid4()),
            userId=user_id,
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

    def get_run(self, run_id: str, *, user_id: str | None = None) -> Run | None:
        path = self._metadata_path(run_id)
        if not path.exists():
            return None
        run = Run.model_validate(json.loads(path.read_text(encoding="utf-8")))
        if user_id is not None and run.userId != user_id:
            return None
        return run

    def save_run(self, run: Run) -> Run:
        self._metadata_path(run.id).write_text(
            json.dumps(run.model_dump(mode="json"), indent=2),
            encoding="utf-8",
        )
        return run

    def append_event(self, run_id: str, event: RunEvent) -> RunEvent:
        run = self._ensure_run_exists(run_id)
        next_sequence = self._get_last_sequence(run_id) + 1
        persisted_event = event.model_copy(
            update={
                "sequence": next_sequence,
                "userId": event.userId if event.userId is not None else run.userId,
            }
        )
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
            userId=run.userId,
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

    def list_snapshots(
        self,
        user_id: str | None = None,
        statuses: list[RunStatus] | None = None,
        kinds: list[RunKind] | None = None,
        limit: int | None = None,
    ) -> list[RunSnapshot]:
        allowed_statuses = set(statuses or [])
        allowed_kinds = set(kinds or [])
        snapshots: list[RunSnapshot] = []

        for path in self._snapshots_dir.glob("*.json"):
            try:
                snapshot = RunSnapshot.model_validate(json.loads(path.read_text(encoding="utf-8")))
            except (ValidationError, json.JSONDecodeError, OSError):
                log.warning("Skipping unreadable run snapshot at %s", path)
                continue
            if user_id is not None and snapshot.userId != user_id:
                continue
            if allowed_statuses and snapshot.status not in allowed_statuses:
                continue
            if allowed_kinds and snapshot.kind not in allowed_kinds:
                continue
            snapshots.append(snapshot)

        snapshots.sort(key=lambda item: item.updatedAt, reverse=True)
        if limit is not None:
            return snapshots[:limit]
        return snapshots

    def list_events_after(
        self,
        run_id: str,
        sequence: int,
        *,
        user_id: str | None = None,
    ) -> list[RunEvent]:
        if user_id is not None and self.get_run(run_id, user_id=user_id) is None:
            return []

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

        append_assistant_event(chat_id, run_id, llm_event)

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
