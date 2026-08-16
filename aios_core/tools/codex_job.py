"""Interactive Codex jobs backed by ``codex app-server``.

The app-server protocol is bidirectional JSON-RPC. That matters because Codex
can pause a turn with ``item/tool/requestUserInput``; the old ``codex exec``
wrapper had no response channel and treated that pause as a subprocess error.

The public start/poll/stop API remains stable. Polling may now return
``status="awaiting_input"`` and a structured ``pending_input``. Answers can be
submitted either through :func:`codex_answer` or the gateway route used by the
desktop client.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import uuid4

from ..runtime_context import get_current_chat_id, resolve_chat_files_path
from .codex_subagent import translate_codex_event

_REPO_ROOT = str(Path(__file__).resolve().parents[2])
_ProgressSink = Callable[[str, str, dict[str, Any]], None]
_progress_sink: _ProgressSink | None = None


def set_progress_sink(sink: _ProgressSink | None) -> None:
    global _progress_sink
    _progress_sink = sink


def _deploy_mcp_config() -> str:
    return (
        'mcp_servers.deploy={command="' + sys.executable + '",'
        'args=["-m","aios_core.deploy.mcp_server"],'
        'env={PYTHONPATH="' + _REPO_ROOT + '"}}'
    )


SAFETY_CAP_SECONDS = float(os.getenv("AIOS_CODEX_SAFETY_CAP", "1800"))
MAX_ACTIVE_JOBS = int(os.getenv("AIOS_CODEX_MAX_JOBS", "6"))
RPC_TIMEOUT_SECONDS = float(os.getenv("AIOS_CODEX_RPC_TIMEOUT", "30"))
_ACTIVE_STATUSES = {"running", "awaiting_input"}
_TERMINAL_STATUSES = {"done", "error", "cancelled"}


def _normalize_app_server_item(item: dict[str, Any]) -> dict[str, Any]:
    """Convert app-server camelCase items to the exec JSONL shape."""
    normalized = dict(item)
    normalized["type"] = {
        "agentMessage": "agent_message",
        "commandExecution": "command_execution",
        "fileChange": "file_change",
        "mcpToolCall": "mcp_tool_call",
        "dynamicToolCall": "dynamic_tool_call",
        "webSearch": "web_search",
    }.get(str(item.get("type")), item.get("type"))
    for source, target in (
        ("aggregatedOutput", "aggregated_output"),
        ("exitCode", "exit_code"),
    ):
        if source in item:
            normalized[target] = item[source]
    return normalized


class _PendingRpc:
    def __init__(self) -> None:
        self.ready = threading.Event()
        self.response: dict[str, Any] | None = None


class CodexJob:
    """One isolated app-server process and one Codex thread/turn."""

    def __init__(
        self,
        job_id: str,
        task: str,
        workdir: str,
        cmd: list[str],
        *,
        model: str | None = None,
        session_id: str | None = None,
        parent_tool_call_id: str | None = None,
    ) -> None:
        self.id = job_id
        self.task = task
        self.workdir = workdir
        self.cmd = cmd
        self.model = model
        self.session_id = session_id
        self.parent_tool_call_id = parent_tool_call_id
        self.status = "running"
        self.error: str | None = None
        self.result: str | None = None
        self.events: list[dict[str, Any]] = []
        self.started_at = monotonic()
        self.finished_at: float | None = None
        self.thread_id: str | None = None
        self.turn_id: str | None = None
        self._final_message: str | None = None
        self._pending_input: dict[str, Any] | None = None
        self._pending_input_request_id: int | str | None = None
        self._proc: subprocess.Popen[str] | None = None
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._new = threading.Event()
        self._next_request_id = 1
        self._pending_rpc: dict[int, _PendingRpc] = {}
        self._stderr_chunks: list[str] = []

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if _progress_sink is None or not self.session_id:
            return
        try:
            _progress_sink(
                self.session_id,
                event_type,
                {
                    "job_id": self.id,
                    "parent_tool_call_id": self.parent_tool_call_id,
                    **payload,
                },
            )
        except Exception:
            pass

    def start(self) -> None:
        self._proc = subprocess.Popen(
            self.cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=self.workdir,
            start_new_session=True,
        )
        self._emit("codex.started", {"task_summary": self.task[:200]})
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        threading.Thread(target=self._bootstrap, daemon=True).start()
        threading.Thread(target=self._watchdog, daemon=True).start()

    def _send(self, message: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.poll() is not None:
            raise RuntimeError("Codex app-server is not running")
        encoded = json.dumps(message, separators=(",", ":")) + "\n"
        with self._write_lock:
            proc.stdin.write(encoded)
            proc.stdin.flush()

    def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            request_id = self._next_request_id
            self._next_request_id += 1
            pending = _PendingRpc()
            self._pending_rpc[request_id] = pending
        try:
            self._send(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            )
            if not pending.ready.wait(RPC_TIMEOUT_SECONDS):
                raise TimeoutError(
                    "Codex app-server did not answer "
                    f"{method} within {RPC_TIMEOUT_SECONDS:g}s"
                )
            response = pending.response or {}
            if response.get("error") is not None:
                raise RuntimeError(f"{method} failed: {response['error']}")
            result = response.get("result")
            return result if isinstance(result, dict) else {}
        finally:
            with self._lock:
                self._pending_rpc.pop(request_id, None)

    def _bootstrap(self) -> None:
        try:
            self._rpc(
                "initialize",
                {
                    "clientInfo": {"name": "mini-aios", "version": "1"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            self._send({"jsonrpc": "2.0", "method": "initialized"})
            thread_params: dict[str, Any] = {
                "cwd": self.workdir,
                "runtimeWorkspaceRoots": [self.workdir],
                "approvalPolicy": "never",
                "sandbox": "danger-full-access",
                "ephemeral": False,
            }
            if self.model:
                thread_params["model"] = self.model
            thread_result = self._rpc("thread/start", thread_params)
            thread = thread_result.get("thread") or {}
            self.thread_id = str(thread.get("id") or "") or None
            if not self.thread_id:
                raise RuntimeError("thread/start returned no thread id")
            turn_result = self._rpc(
                "turn/start",
                {
                    "threadId": self.thread_id,
                    "input": [{"type": "text", "text": self.task, "text_elements": []}],
                },
            )
            turn = turn_result.get("turn") or {}
            self.turn_id = str(turn.get("id") or "") or None
            self._new.set()
        except Exception as exc:
            self._finish("error", error=str(exc))

    def _read_stdout(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        try:
            for line in iter(proc.stdout.readline, ""):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    message = json.loads(stripped)
                except json.JSONDecodeError:
                    self._stderr_chunks.append(stripped)
                    continue
                self._handle_message(message)
        except Exception as exc:
            if self.status not in _TERMINAL_STATUSES:
                self._finish("error", error=f"Codex protocol reader failed: {exc}")
        finally:
            returncode = proc.wait()
            if self.status not in _TERMINAL_STATUSES:
                detail = "".join(self._stderr_chunks).strip()
                self._finish(
                    "error", error=detail or f"Codex app-server exited {returncode}"
                )

    def _read_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for line in iter(proc.stderr.readline, ""):
            if line:
                self._stderr_chunks.append(line)

    def _handle_message(self, message: Any) -> None:
        if not isinstance(message, dict):
            return
        response_id = message.get("id")
        if response_id is not None and "method" not in message:
            with self._lock:
                pending = self._pending_rpc.get(response_id)
                if pending is not None:
                    pending.response = message
                    pending.ready.set()
            return

        method = message.get("method")
        params = (
            message.get("params") if isinstance(message.get("params"), dict) else {}
        )
        if method == "item/tool/requestUserInput" and response_id is not None:
            self._request_user_input(response_id, params)
        elif method in {"item/started", "item/completed"}:
            self._handle_item(method, params)
        elif method == "turn/started":
            turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
            self.turn_id = str(turn.get("id") or self.turn_id or "") or None
        elif method == "turn/completed":
            self._handle_turn_completed(params)

    def _handle_item(self, method: str, params: dict[str, Any]) -> None:
        item = params.get("item")
        if not isinstance(item, dict):
            return
        event_type = "item.started" if method.endswith("started") else "item.completed"
        for desc in translate_codex_event(
            {"type": event_type, "item": _normalize_app_server_item(item)}
        ):
            if desc["kind"] == "text":
                self._final_message = desc["value"]
                self._emit(
                    "codex.progress", {"kind": "message", "detail": desc["value"][:500]}
                )
                continue
            with self._lock:
                self.events.append(desc)
            self._new.set()
            tool = desc.get("tool_name", "tool")
            kind = (
                "command"
                if tool == "command_execution"
                else "file"
                if tool == "file_change"
                else tool
            )
            self._emit(
                "codex.progress",
                {
                    "kind": kind,
                    "phase": desc["kind"],
                    "tool_call_id": desc.get("tool_call_id"),
                    "detail": str(desc.get("input") or desc.get("output") or "")[:500],
                },
            )

    def _request_user_input(
        self, request_id: int | str, params: dict[str, Any]
    ) -> None:
        questions = (
            params.get("questions") if isinstance(params.get("questions"), list) else []
        )
        pending_input = {
            "item_id": params.get("itemId"),
            "thread_id": params.get("threadId"),
            "turn_id": params.get("turnId"),
            "is_blocking": bool(params.get("isBlocking", True)),
            "questions": questions,
        }
        event = {"kind": "input_requested", "input": pending_input}
        with self._lock:
            if self._pending_input_request_id is not None:
                self._send_error_response(
                    request_id, -32000, "another input request is already pending"
                )
                return
            self._pending_input_request_id = request_id
            self._pending_input = pending_input
            self.status = "awaiting_input"
            self.events.append(event)
        self._new.set()
        self._emit("codex.input.requested", pending_input)

    def _send_error_response(
        self, request_id: int | str, code: int, message: str
    ) -> None:
        try:
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": code, "message": message},
                }
            )
        except Exception:
            pass

    @staticmethod
    def _normalize_answers(answers: dict[str, Any]) -> dict[str, dict[str, list[str]]]:
        normalized: dict[str, dict[str, list[str]]] = {}
        for question_id, value in answers.items():
            if isinstance(value, dict):
                value = value.get("answers", [])
            if isinstance(value, str):
                values = [value]
            elif isinstance(value, list):
                values = [str(item) for item in value]
            else:
                values = [str(value)]
            normalized[str(question_id)] = {"answers": values}
        return normalized

    def answer(self, answers: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(answers, dict) or not answers:
            return {"error": "answers must be a non-empty object"}
        with self._lock:
            request_id = self._pending_input_request_id
            pending_input = self._pending_input
        if request_id is None or pending_input is None:
            return {"error": f"job {self.id} is not awaiting input"}
        normalized = self._normalize_answers(answers)
        expected = {
            str(q.get("id")) for q in pending_input.get("questions", []) if q.get("id")
        }
        missing = sorted(expected - normalized.keys())
        if missing:
            return {"error": f"missing answers for: {', '.join(missing)}"}
        try:
            self._send(
                {"jsonrpc": "2.0", "id": request_id, "result": {"answers": normalized}}
            )
        except Exception as exc:
            return {"error": f"failed to answer Codex: {exc}"}
        with self._lock:
            self._pending_input_request_id = None
            self._pending_input = None
            if self.status == "awaiting_input":
                self.status = "running"
        self._new.set()
        self._emit("codex.input.resolved", {"question_ids": sorted(normalized)})
        return {"job_id": self.id, "status": self.status}

    def _handle_turn_completed(self, params: dict[str, Any]) -> None:
        turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
        for item in turn.get("items") or []:
            if (
                isinstance(item, dict)
                and item.get("type") == "agentMessage"
                and item.get("text")
            ):
                self._final_message = str(item["text"])
        turn_status = turn.get("status")
        if turn_status == "completed":
            self._finish("done", result=self._final_message or "(empty)")
        elif turn_status == "interrupted":
            self._finish("cancelled", error="Codex turn was interrupted")
        else:
            turn_error = (
                turn.get("error") if isinstance(turn.get("error"), dict) else {}
            )
            self._finish(
                "error", error=str(turn_error.get("message") or "Codex turn failed")
            )

    def _finish(
        self, status: str, *, error: str | None = None, result: str | None = None
    ) -> None:
        with self._lock:
            if self.status in _TERMINAL_STATUSES:
                return
            self.status = status
            self.error = error
            self.result = result
            self.finished_at = monotonic()
            self._pending_input = None
            self._pending_input_request_id = None
            pending_calls = list(self._pending_rpc.values())
        for pending in pending_calls:
            pending.ready.set()
        self._new.set()
        self._emit(
            "codex.completed", {"status": status, "result": result, "error": error}
        )
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                proc.kill()

    def _watchdog(self) -> None:
        deadline = self.started_at + SAFETY_CAP_SECONDS
        while True:
            with self._lock:
                if self.status in _TERMINAL_STATUSES:
                    return
            remaining = deadline - monotonic()
            if remaining <= 0:
                self._finish(
                    "error", error=f"Codex exceeded safety cap {SAFETY_CAP_SECONDS:g}s"
                )
                return
            self._new.wait(timeout=min(1.0, remaining))
            self._new.clear()

    def poll(self, cursor: int = 0, wait: float = 0.0) -> dict[str, Any]:
        cursor = max(0, int(cursor))
        if wait and wait > 0:
            end = monotonic() + float(wait)
            while monotonic() < end:
                with self._lock:
                    ready = cursor < len(self.events) or self.status != "running"
                if ready:
                    break
                self._new.wait(timeout=min(0.5, max(0.0, end - monotonic())))
                self._new.clear()
        with self._lock:
            return {
                "job_id": self.id,
                "status": self.status,
                "thread_id": self.thread_id,
                "turn_id": self.turn_id,
                "events": self.events[cursor:],
                "cursor": len(self.events),
                "pending_input": self._pending_input,
                "result": self.result if self.status == "done" else None,
                "error": self.error,
            }

    def stop(self) -> None:
        thread_id, turn_id = self.thread_id, self.turn_id
        if thread_id and turn_id and self.status in _ACTIVE_STATUSES:
            try:
                self._rpc("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
            except Exception:
                pass
        self._finish("cancelled", error="stopped by request")

    def summary(self) -> dict[str, Any]:
        with self._lock:
            return {
                "job_id": self.id,
                "session_id": self.session_id,
                "status": self.status,
                "task": self.task[:80],
                "events": len(self.events),
                "pending_input": self._pending_input,
            }


class CodexJobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, CodexJob] = {}
        self._lock = threading.Lock()

    def _active_count(self) -> int:
        return sum(1 for job in self._jobs.values() if job.status in _ACTIVE_STATUSES)

    def start(
        self,
        task: str,
        path: str = ".",
        model: str | None = None,
        enable_deploy: bool = True,
        session_id: str | None = None,
        parent_tool_call_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(task, str) or not task.strip():
            return {"error": "task is required"}
        if not isinstance(path, str) or not path.strip():
            return {"error": "path must be a non-empty string"}
        workdir = resolve_chat_files_path(path.strip())
        if not workdir.exists():
            return {"error": f"path does not exist: {workdir}"}
        if not workdir.is_dir():
            return {"error": f"path is not a directory: {workdir}"}

        cmd = [
            "codex",
            "app-server",
            "--stdio",
            "--enable",
            "default_mode_request_user_input",
        ]
        if enable_deploy:
            cmd.extend(["-c", _deploy_mcp_config()])
        job_id = uuid4().hex[:12]
        job = CodexJob(
            job_id,
            task.strip(),
            str(workdir),
            cmd,
            model=model.strip() if isinstance(model, str) and model.strip() else None,
            session_id=session_id,
            parent_tool_call_id=parent_tool_call_id,
        )
        with self._lock:
            if self._active_count() >= MAX_ACTIVE_JOBS:
                running = [
                    jid
                    for jid, item in self._jobs.items()
                    if item.status in _ACTIVE_STATUSES
                ]
                return {
                    "error": (
                        f"too many active codex jobs ({MAX_ACTIVE_JOBS}); "
                        f"running: {running}"
                    )
                }
            conflicting = next(
                (
                    item.id
                    for item in self._jobs.values()
                    if item.status in _ACTIVE_STATUSES and item.workdir == str(workdir)
                ),
                None,
            )
            if conflicting:
                return {
                    "error": (
                        f"Codex job {conflicting} is already editing {workdir}; "
                        "wait for it to finish or stop it before starting another"
                    )
                }
            self._jobs[job_id] = job
        try:
            job.start()
        except FileNotFoundError:
            with self._lock:
                self._jobs.pop(job_id, None)
            return {"error": "codex CLI is not installed or not on PATH"}
        except Exception as exc:
            with self._lock:
                self._jobs.pop(job_id, None)
            return {"error": f"failed to start codex -- {exc}"}
        return {"job_id": job_id, "status": "running", "workdir": str(workdir)}

    def get(self, job_id: str) -> CodexJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def poll(self, job_id: str, cursor: int = 0, wait: float = 0.0) -> dict[str, Any]:
        job = self.get(job_id)
        return (
            job.poll(cursor=cursor, wait=wait)
            if job
            else {"error": f"unknown job_id: {job_id}"}
        )

    def answer(self, job_id: str, answers: dict[str, Any]) -> dict[str, Any]:
        job = self.get(job_id)
        return job.answer(answers) if job else {"error": f"unknown job_id: {job_id}"}

    def stop(self, job_id: str) -> dict[str, Any]:
        job = self.get(job_id)
        if job is None:
            return {"error": f"unknown job_id: {job_id}"}
        job.stop()
        return {"job_id": job_id, "status": job.status}

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [job.summary() for job in self._jobs.values()]

    def list_for_session(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [
                job.summary()
                for job in self._jobs.values()
                if job.session_id == session_id
            ]


_manager = CodexJobManager()


def codex_start(
    task: str | None = None, path: str = ".", model: str | None = None, fc=None
) -> dict[str, Any]:
    """Start an interactive Codex coding job in the background."""
    return _manager.start(
        task or "",
        path=path,
        model=model,
        enable_deploy=True,
        session_id=get_current_chat_id(),
        parent_tool_call_id=str(getattr(fc, "call_id", None))
        if getattr(fc, "call_id", None)
        else None,
    )


def codex_poll(
    job_id: str | None = None, cursor: int = 0, wait: float = 0.0, fc=None
) -> dict[str, Any]:
    """Poll a job. ``awaiting_input`` includes a structured ``pending_input``."""
    return _manager.poll(job_id or "", cursor=cursor, wait=wait)


def codex_answer(
    job_id: str | None = None, answers: dict[str, Any] | None = None, fc=None
) -> dict[str, Any]:
    """Answer the questions in a Codex job's ``pending_input`` object."""
    return _manager.answer(job_id or "", answers or {})


def codex_stop(job_id: str | None = None, fc=None) -> dict[str, Any]:
    """Interrupt and stop a Codex job."""
    return _manager.stop(job_id or "")
