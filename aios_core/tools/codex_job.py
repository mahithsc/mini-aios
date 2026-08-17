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
import logging
import os
import signal
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import uuid4

from ..deploy.manifest import (
    ManifestValidationError,
    find_deployment_root,
    load_deployment_manifest,
)
from ..runtime_context import (
    get_current_chat_id,
    get_current_run_id,
    resolve_codex_workdir,
)
from .codex_run_store import CodexRunStore
from .codex_subagent import translate_codex_event


# Compatibility seam retained for tests and callers that previously patched
# this module-level resolver.
def resolve_chat_files_path(path: str) -> Path:
    return resolve_codex_workdir(path)

_REPO_ROOT = str(Path(__file__).resolve().parents[2])
_ProgressSink = Callable[[str, str, dict[str, Any]], None]
_progress_sink: _ProgressSink | None = None
_LifecycleSink = Callable[[str, str, str], None]
_lifecycle_sink: _LifecycleSink | None = None
log = logging.getLogger(__name__)


def set_progress_sink(sink: _ProgressSink | None) -> None:
    global _progress_sink
    _progress_sink = sink


def set_lifecycle_sink(sink: _LifecycleSink | None) -> None:
    global _lifecycle_sink
    _lifecycle_sink = sink


def _deploy_mcp_config() -> str:
    return (
        'mcp_servers.deploy={command="' + sys.executable + '",'
        'args=["-m","aios_core.deploy.mcp_server"],'
        'env={PYTHONPATH="' + _REPO_ROOT + '"}}'
    )


SAFETY_CAP_SECONDS = float(os.getenv("AIOS_CODEX_SAFETY_CAP", "1800"))
MAX_ACTIVE_JOBS = int(os.getenv("AIOS_CODEX_MAX_JOBS", "6"))
RPC_TIMEOUT_SECONDS = float(os.getenv("AIOS_CODEX_RPC_TIMEOUT", "30"))
MAX_EVENT_TEXT_CHARS = int(os.getenv("AIOS_CODEX_EVENT_TEXT_LIMIT", "50000"))
MAX_STDERR_CHARS = int(os.getenv("AIOS_CODEX_STDERR_LIMIT", "100000"))
RETENTION_DAYS = int(os.getenv("AIOS_CODEX_RETENTION_DAYS", "30"))
MAX_RECOVERY_ATTEMPTS = int(os.getenv("AIOS_CODEX_MAX_RECOVERIES", "2"))
MAX_DEPLOY_FOLLOWUPS = int(os.getenv("AIOS_CODEX_MAX_DEPLOY_FOLLOWUPS", "2"))
_ACTIVE_STATUSES = {"running", "awaiting_input"}
_TERMINAL_STATUSES = {"done", "error", "cancelled"}
_DEPLOY_TOOL_BY_COMPONENT = {
    "database": "deploy_database",
    "server": "deploy_server",
    "frontend": "deploy_frontend",
}


def _deployment_contract_task(task: str) -> str:
    """Attach the non-optional contract for a deploy-enabled Codex run."""

    return f"""{task.strip()}

AIOS CLOUD DEPLOYMENT CONTRACT (MANDATORY)
The user requested a real cloud deployment; building files alone is not completion.
1. Work from the app root containing aios.deploy.yaml. Create or correct that manifest so it declares every component this task actually builds.
2. Use only the tools on the `deploy` MCP server for deployment. Never use built-in hosting/deployment tools or provider CLIs.
3. Call every matching tool for every declared component, in dependency order: deploy_database, then deploy_server, then deploy_frontend. Skip only components that are absent from the manifest.
4. A queued/building response with a deployment ID proves the deployment was created. Call check_app_status with the manifest app_id to inspect every component, its queue/build/failure state, latest event, and artifact upload/verification state. Use get_deployment_status and get_deployment_events for deeper per-job diagnostics. Do not wait forever on infrastructure that remains queued.
5. If a deploy call returns an actionable artifact/manifest validation error, fix the artifact and retry the same deploy tool. Treat secrets as blockers only when the AIOS tool itself returns awaiting_secrets; do not invent a secret requirement.
6. Do not finish with 'deployment was not performed.' Before finishing, report each deploy tool called, its deployment ID, and its current status. If an AIOS tool still cannot create a deployment, report its exact response.
"""


def _mcp_result_payload(result: Any) -> dict[str, Any] | None:
    """Extract a JSON object from an app-server MCP result envelope."""

    if not isinstance(result, dict):
        return None
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        nested = structured.get("result")
        return nested if isinstance(nested, dict) else structured
    content = result.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            try:
                payload = json.loads(str(block.get("text") or ""))
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                return payload
    # Keeping direct dictionaries supported makes the protocol adapter tolerant
    # of lightweight MCP clients and deterministic test servers.
    return result


def _process_identity(pid: int) -> str | None:
    """Return a PID-reuse-safe identity for a locally running process."""

    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value or None


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
        store: CodexRunStore,
        enable_deploy: bool = False,
        deploy_state: dict[str, Any] | None = None,
        resume_thread_id: str | None = None,
        recovery_prompt: str | None = None,
    ) -> None:
        self.id = job_id
        self.task = task
        self.workdir = workdir
        self.cmd = cmd
        self.model = model
        self.session_id = session_id
        self.parent_tool_call_id = parent_tool_call_id
        self.store = store
        self.enable_deploy = enable_deploy
        self.resume_thread_id = resume_thread_id
        self.recovery_prompt = recovery_prompt
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
        self._finishing = False
        restored_deploy_state = deploy_state or {}
        self._deploy_tools_called = {
            str(tool) for tool in restored_deploy_state.get("called", [])
        }
        self._deploy_tools_enqueued = {
            str(tool) for tool in restored_deploy_state.get("enqueued", [])
        }
        restored_results = restored_deploy_state.get("last_results")
        self._deploy_last_results = (
            dict(restored_results) if isinstance(restored_results, dict) else {}
        )
        self._deploy_followups = int(restored_deploy_state.get("followups") or 0)

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if not self.session_id:
            return
        enriched = {
            "job_id": self.id,
            "parent_tool_call_id": self.parent_tool_call_id,
            **payload,
        }
        try:
            sequence = self.store.append_gateway_event(
                self.id, self.session_id, event_type, enriched
            )
        except Exception:
            log.exception("Failed to persist Codex gateway event %s", event_type)
            return
        if _progress_sink is None:
            return
        enriched["codex_event_id"] = f"{self.id}:{sequence}"
        try:
            _progress_sink(self.session_id, event_type, enriched)
            self.store.complete_gateway_event(self.id, sequence)
        except Exception:
            log.exception("Failed to publish Codex gateway event %s", event_type)

    def _emit_lifecycle(self, status: str) -> None:
        if _lifecycle_sink is None or not self.session_id:
            return
        try:
            _lifecycle_sink(self.session_id, self.id, status)
        except Exception:
            pass

    def _append_event(self, event: dict[str, Any]) -> None:
        bounded = dict(event)
        for key in ("input", "output", "error"):
            value = bounded.get(key)
            if isinstance(value, str) and len(value) > MAX_EVENT_TEXT_CHARS:
                bounded[key] = (
                    value[:MAX_EVENT_TEXT_CHARS]
                    + "\n... (truncated by Codex event limit)"
                )
        with self._lock:
            self.events.append(bounded)
        self.store.append_event(self.id, bounded)
        self._new.set()

    def _append_stderr(self, text: str) -> None:
        with self._lock:
            self._stderr_chunks.append(text)
            total = sum(len(chunk) for chunk in self._stderr_chunks)
            while self._stderr_chunks and total > MAX_STDERR_CHARS:
                total -= len(self._stderr_chunks.pop(0))

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
        pid = getattr(self._proc, "pid", None)
        if isinstance(pid, int):
            self.store.update(
                self.id,
                process_pid=pid,
                process_identity=_process_identity(pid),
            )
        log.info(
            "Codex job started",
            extra={
                "codex_job_id": self.id,
                "codex_session_id": self.session_id,
                "codex_recovered": bool(self.resume_thread_id),
            },
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
            method = "thread/start"
            if self.resume_thread_id:
                method = "thread/resume"
                thread_params["threadId"] = self.resume_thread_id
            thread_result = self._rpc(method, thread_params)
            thread = thread_result.get("thread") or {}
            self.thread_id = str(
                thread.get("id") or self.resume_thread_id or ""
            ) or None
            if not self.thread_id:
                raise RuntimeError("thread/start returned no thread id")
            turn_result = self._rpc(
                "turn/start",
                {
                    "threadId": self.thread_id,
                    "input": [
                        {
                            "type": "text",
                            "text": self.recovery_prompt or self.task,
                            "text_elements": [],
                        }
                    ],
                },
            )
            turn = turn_result.get("turn") or {}
            self.turn_id = str(turn.get("id") or "") or None
            self.store.update(
                self.id, thread_id=self.thread_id, turn_id=self.turn_id
            )
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
                    self._append_stderr(stripped)
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
                self._append_stderr(line)

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
            if self.turn_id:
                self.store.update(self.id, turn_id=self.turn_id)
        elif method == "turn/completed":
            self._handle_turn_completed(params)

    def _handle_item(self, method: str, params: dict[str, Any]) -> None:
        item = params.get("item")
        if not isinstance(item, dict):
            return
        if method == "item/completed" and item.get("type") == "mcpToolCall":
            server = str(item.get("server") or "")
            tool = str(item.get("tool") or item.get("name") or "")
            if server == "deploy" and tool in _DEPLOY_TOOL_BY_COMPONENT.values():
                with self._lock:
                    self._deploy_tools_called.add(tool)
                    payload = _mcp_result_payload(item.get("result"))
                    self._deploy_last_results[tool] = payload or item.get("error") or {
                        "status": str(item.get("status") or "unknown")
                    }
                    deployment_id = payload.get("id") if payload else None
                    if isinstance(deployment_id, str) and deployment_id.startswith(
                        "dep_"
                    ):
                        self._deploy_tools_enqueued.add(tool)
                    self._persist_deploy_state()
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
            self._append_event(desc)
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
        with self._lock:
            if self._pending_input_request_id is not None:
                self._send_error_response(
                    request_id, -32000, "another input request is already pending"
                )
                return
            self._pending_input_request_id = request_id
            self._pending_input = pending_input
            self.status = "awaiting_input"
        self._append_event({"kind": "input_requested", "input": pending_input})
        self.store.update(
            self.id, status="awaiting_input", pending_input=pending_input
        )
        self.store.enqueue_signal(self.id, "awaiting_input")
        log.info(
            "Codex job awaiting input",
            extra={"codex_job_id": self.id, "codex_session_id": self.session_id},
        )
        self._emit("codex.input.requested", pending_input)
        self._emit_lifecycle("awaiting_input")

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
        self.store.update(
            self.id, status="running", clear_pending_input=True
        )
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
            issue = self._deployment_guard_issue()
            if issue is not None:
                if self._deploy_followups < MAX_DEPLOY_FOLLOWUPS:
                    self._deploy_followups += 1
                    self._persist_deploy_state()
                    self._append_event(
                        {
                            "kind": "deployment_guard",
                            "output": issue,
                            "attempt": self._deploy_followups,
                        }
                    )
                    self._emit(
                        "codex.progress",
                        {
                            "kind": "deployment_guard",
                            "phase": "continuing",
                            "detail": issue[:500],
                        },
                    )
                    threading.Thread(
                        target=self._start_deploy_followup,
                        args=(issue,),
                        daemon=True,
                    ).start()
                    return
                self._finish(
                    "error",
                    error=(
                        "Codex did not satisfy the mandatory AIOS deployment "
                        f"contract after {MAX_DEPLOY_FOLLOWUPS} follow-up turns: {issue}"
                    ),
                )
                return
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

    def _deployment_guard_issue(self) -> str | None:
        if not self.enable_deploy:
            return None
        root = find_deployment_root(self.workdir)
        if root is None:
            return (
                "No aios.deploy.yaml was found in the working directory or its "
                "ancestors. Create the manifest at the app root and perform the "
                "matching AIOS MCP deployments."
            )
        try:
            manifest = load_deployment_manifest(root)
        except ManifestValidationError as exc:
            return f"The deployment manifest is invalid: {exc}"
        required = {
            tool
            for component, tool in _DEPLOY_TOOL_BY_COMPONENT.items()
            if getattr(manifest, component) is not None
        }
        with self._lock:
            uncalled = sorted(required - self._deploy_tools_called)
            not_enqueued = sorted(required - self._deploy_tools_enqueued)
            last_results = {
                tool: self._deploy_last_results.get(tool) for tool in not_enqueued
            }
        if not not_enqueued:
            return None
        if uncalled:
            detail = f"were not made: {', '.join(uncalled)}"
        else:
            detail = (
                "did not return a deployment ID: " + ", ".join(not_enqueued)
            )
            detail += ". Last AIOS responses: " + json.dumps(
                last_results, default=str, sort_keys=True
            )
        return (
            f"Manifest {root / 'aios.deploy.yaml'} requires these AIOS MCP calls "
            f"that {detail}. Call them now in database, "
            "server, frontend dependency order and report their exact responses."
        )

    def _persist_deploy_state(self) -> None:
        with self._lock:
            deploy_state = {
                "called": sorted(self._deploy_tools_called),
                "enqueued": sorted(self._deploy_tools_enqueued),
                "followups": self._deploy_followups,
                "last_results": dict(self._deploy_last_results),
            }
        self.store.update(
            self.id,
            deploy_state=deploy_state,
        )

    def _start_deploy_followup(self, issue: str) -> None:
        try:
            thread_id = self.thread_id
            if not thread_id:
                raise RuntimeError("Codex thread id is unavailable")
            with self._lock:
                self._final_message = None
            result = self._rpc(
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": [
                        {
                            "type": "text",
                            "text": (
                                "The host rejected completion because the mandatory "
                                f"AIOS deployment postcondition is unmet. {issue} "
                                "Continue in this same workspace. Do not merely explain "
                                "what should be called: make the required deploy MCP calls."
                            ),
                            "text_elements": [],
                        }
                    ],
                },
            )
            turn = result.get("turn") or {}
            self.turn_id = str(turn.get("id") or self.turn_id or "") or None
            self.store.update(self.id, turn_id=self.turn_id)
            self._new.set()
        except Exception as exc:
            self._finish(
                "error", error=f"Could not continue Codex for deployment: {exc}"
            )

    def _finish(
        self, status: str, *, error: str | None = None, result: str | None = None
    ) -> None:
        with self._lock:
            if self.status in _TERMINAL_STATUSES or self._finishing:
                return
            self._finishing = True
            pending_calls = list(self._pending_rpc.values())
        self._terminate_live_process()
        persistence_error: Exception | None = None
        try:
            self.store.update(
                self.id,
                status=status,
                clear_pending_input=True,
                result=result,
                error=error,
                terminal=True,
                clear_process=True,
            )
            if status in {"done", "error"}:
                self.store.enqueue_signal(self.id, status)
        except Exception as exc:
            persistence_error = exc
            log.exception("Failed to persist terminal Codex state for %s", self.id)
        for pending in pending_calls:
            pending.ready.set()
        self._new.set()
        self._emit(
            "codex.completed", {"status": status, "result": result, "error": error}
        )
        if status in {"done", "error"}:
            self._emit_lifecycle(status)
        log.info(
            "Codex job finished",
            extra={
                "codex_job_id": self.id,
                "codex_session_id": self.session_id,
                "codex_status": status,
                "codex_duration_ms": int((monotonic() - self.started_at) * 1000),
            },
        )
        with self._lock:
            self.status = status
            self.error = error
            self.result = result
            self.finished_at = monotonic()
            self._pending_input = None
            self._pending_input_request_id = None
            self._finishing = False
        if persistence_error is not None:
            self.error = self.error or f"failed to persist terminal state: {persistence_error}"

    def _terminate_live_process(self) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        try:
            pid = getattr(proc, "pid", None)
            if isinstance(pid, int):
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            else:
                proc.terminate()
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

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
                events, _ = self.store.events_after(self.id, cursor)
                with self._lock:
                    ready = bool(events) or self.status != "running"
                if ready:
                    break
                self._new.wait(timeout=min(0.5, max(0.0, end - monotonic())))
                self._new.clear()
        events, next_cursor = self.store.events_after(self.id, cursor)
        with self._lock:
            return {
                "job_id": self.id,
                "status": self.status,
                "thread_id": self.thread_id,
                "turn_id": self.turn_id,
                "events": events,
                "cursor": next_cursor,
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
    def __init__(self, store: CodexRunStore | None = None) -> None:
        self._jobs: dict[str, CodexJob] = {}
        self._lock = threading.Lock()
        self.store = store or CodexRunStore(":memory:")

    @staticmethod
    def _command(enable_deploy: bool) -> list[str]:
        cmd = [
            "codex",
            "app-server",
            "--stdio",
            "--enable",
            "default_mode_request_user_input",
        ]
        if enable_deploy:
            cmd.extend(["-c", _deploy_mcp_config()])
        return cmd

    def start(
        self,
        task: str,
        path: str = ".",
        model: str | None = None,
        enable_deploy: bool = False,
        session_id: str | None = None,
        parent_tool_call_id: str | None = None,
        parent_run_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(task, str) or not task.strip():
            return {"error": "task is required"}
        if not isinstance(path, str) or not path.strip():
            return {"error": "path must be a non-empty string"}
        try:
            workdir = resolve_chat_files_path(path.strip()).resolve()
        except ValueError as exc:
            return {"error": str(exc)}
        if not workdir.exists():
            return {"error": f"path does not exist: {workdir}"}
        if not workdir.is_dir():
            return {"error": f"path is not a directory: {workdir}"}

        if enable_deploy:
            # A component path is a convenient place to build from, but the AIOS
            # artifact boundary is always the nearest manifest-bearing app root.
            workdir = find_deployment_root(workdir) or workdir
            effective_task = _deployment_contract_task(task)
        else:
            effective_task = task.strip()

        cmd = self._command(enable_deploy)
        job_id = uuid4().hex[:12]
        job = CodexJob(
            job_id,
            effective_task,
            str(workdir),
            cmd,
            model=model.strip() if isinstance(model, str) and model.strip() else None,
            session_id=session_id,
            parent_tool_call_id=parent_tool_call_id,
            store=self.store,
            enable_deploy=enable_deploy,
        )
        with self._lock:
            durable_active = {
                str(record["job_id"]): record for record in self.store.active()
            }
            live_active = {
                jid: item
                for jid, item in self._jobs.items()
                if item.status in _ACTIVE_STATUSES
            }
            active_ids = set(durable_active) | set(live_active)
            if len(active_ids) >= MAX_ACTIVE_JOBS:
                return {
                    "error": (
                        f"too many active codex jobs ({MAX_ACTIVE_JOBS}); "
                        f"running: {sorted(active_ids)}"
                    )
                }
            conflicting = next(
                (
                    item.id
                    for item in live_active.values()
                    if item.workdir == str(workdir)
                ),
                None,
            )
            if conflicting is None:
                conflicting = next(
                    (
                        job_id
                        for job_id, record in durable_active.items()
                        if record.get("workdir") == str(workdir)
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
            self.store.create(
                job_id=job_id,
                session_id=session_id,
                parent_run_id=parent_run_id,
                parent_tool_call_id=parent_tool_call_id,
                task=effective_task,
                workdir=str(workdir),
                model=job.model,
                capabilities=["filesystem", "shell"]
                + (["cloud_deploy"] if enable_deploy else []),
            )
        try:
            job.start()
        except FileNotFoundError:
            with self._lock:
                self._jobs.pop(job_id, None)
            self.store.update(
                job_id,
                status="error",
                error="codex CLI is not installed or not on PATH",
                terminal=True,
            )
            return {"error": "codex CLI is not installed or not on PATH"}
        except Exception as exc:
            with self._lock:
                self._jobs.pop(job_id, None)
            self.store.update(
                job_id, status="error", error=str(exc), terminal=True
            )
            return {"error": f"failed to start codex -- {exc}"}
        return {
            "job_id": job_id,
            "status": "running",
            "workdir": str(workdir),
            "auto_continuation": bool(session_id),
        }

    def get(self, job_id: str) -> CodexJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def poll(
        self,
        job_id: str,
        cursor: int = 0,
        wait: float = 0.0,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        job = self.get(job_id)
        if job is not None:
            if session_id is not None and job.session_id != session_id:
                return {"error": f"unknown job_id: {job_id}"}
            result = job.poll(
                cursor=cursor, wait=min(max(float(wait), 0.0), 30.0)
            )
            record = self.store.get(job_id) or {}
            for key in (
                "display_status",
                "recovery_count",
                "verification_status",
                "created_at",
                "updated_at",
            ):
                if key in record:
                    result[key] = record[key]
            return result
        record = self.store.get(job_id)
        if record is None or (
            session_id is not None and record.get("session_id") != session_id
        ):
            return {"error": f"unknown job_id: {job_id}"}
        events, next_cursor = self.store.events_after(job_id, cursor)
        return {
            **record,
            "events": events,
            "cursor": next_cursor,
        }

    def answer(
        self,
        job_id: str,
        answers: dict[str, Any],
        session_id: str | None = None,
    ) -> dict[str, Any]:
        job = self.get(job_id)
        if job is not None:
            if session_id is not None and job.session_id != session_id:
                return {"error": f"unknown job_id: {job_id}"}
            return job.answer(answers)
        record = self.store.get(job_id)
        if (
            record is None
            or (session_id is not None and record.get("session_id") != session_id)
            or record.get("status") != "awaiting_input"
        ):
            return {"error": f"unknown job_id: {job_id}"}
        pending = record.get("pending_input") or {}
        expected = {
            str(question.get("id"))
            for question in pending.get("questions", [])
            if isinstance(question, dict) and question.get("id")
        }
        missing = sorted(expected - {str(key) for key in answers})
        if missing:
            return {"error": f"missing answers for: {', '.join(missing)}"}
        prompt = (
            "The server restarted while you were waiting for input. Resume the "
            "delegated task from the current workspace and thread. The user supplied "
            f"these answers to your prior questions: {json.dumps(answers, default=str)}. "
            "Inspect existing work before acting, avoid repeating completed external "
            "side effects, finish the task, and report verification performed."
        )
        self._terminate_recorded_process(record)
        return self._recover_record(record, recovery_prompt=prompt)

    def stop(self, job_id: str, session_id: str | None = None) -> dict[str, Any]:
        job = self.get(job_id)
        if job is not None:
            if session_id is not None and job.session_id != session_id:
                return {"error": f"unknown job_id: {job_id}"}
            job.stop()
            return {"job_id": job_id, "status": job.status}
        record = self.store.get(job_id)
        if (
            record is None
            or (session_id is not None and record.get("session_id") != session_id)
            or record.get("status") not in _ACTIVE_STATUSES
        ):
            return {"error": f"unknown job_id: {job_id}"}
        self._terminate_recorded_process(record)
        self.store.update(
            job_id,
            status="cancelled",
            error="stopped by request",
            terminal=True,
            clear_process=True,
            clear_pending_input=True,
        )
        self.emit_status(
            job_id,
            "codex.completed",
            {"status": "cancelled", "error": "stopped by request", "result": None},
        )
        return {"job_id": job_id, "status": "cancelled"}

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [job.summary() for job in self._jobs.values()]

    def list_for_session(self, session_id: str) -> list[dict[str, Any]]:
        return self.store.list_for_session(session_id)

    def stop_for_session(self, session_id: str) -> list[str]:
        with self._lock:
            jobs = [
                job
                for job in self._jobs.values()
                if job.session_id == session_id and job.status in _ACTIVE_STATUSES
            ]
        for job in jobs:
            job.stop()
        stopped = [job.id for job in jobs]
        live_ids = set(stopped)
        for record in self.store.active():
            job_id = str(record["job_id"])
            if record.get("session_id") == session_id and job_id not in live_ids:
                result = self.stop(job_id, session_id=session_id)
                if "error" not in result:
                    stopped.append(job_id)
        return stopped

    def stop_all(self) -> list[str]:
        with self._lock:
            jobs = [job for job in self._jobs.values() if job.status in _ACTIVE_STATUSES]
        for job in jobs:
            job.stop()
        stopped = [job.id for job in jobs]
        live_ids = set(stopped)
        for record in self.store.active():
            job_id = str(record["job_id"])
            if job_id not in live_ids:
                result = self.stop(job_id)
                if "error" not in result:
                    stopped.append(job_id)
        return stopped

    def reconcile_stale(self) -> list[str]:
        recovered: list[str] = []
        live_ids = set(self._jobs)
        for record in self.store.active():
            job_id = str(record["job_id"])
            if job_id in live_ids:
                continue
            self._terminate_recorded_process(record)
            if record.get("status") == "awaiting_input":
                self.store.update(job_id, clear_process=True)
                self.store.enqueue_signal(job_id, "awaiting_input")
                continue
            result = self._recover_record(record)
            if "error" not in result:
                recovered.append(job_id)
        return recovered

    def _recover_record(
        self,
        record: dict[str, Any],
        *,
        recovery_prompt: str | None = None,
    ) -> dict[str, Any]:
        job_id = str(record["job_id"])
        thread_id = record.get("thread_id")
        attempts = int(record.get("recovery_count") or 0)
        if not thread_id:
            return self._fail_recovery(job_id, "Codex thread id was not persisted.")
        if attempts >= MAX_RECOVERY_ATTEMPTS:
            return self._fail_recovery(
                job_id,
                f"Codex exceeded {MAX_RECOVERY_ATTEMPTS} recovery attempts.",
            )
        prompt = recovery_prompt or (
            "The host server restarted during this delegated task. Resume from the "
            "current workspace and existing thread. Inspect what is already complete, "
            "avoid repeating completed external side effects, finish the original task, "
            "and run proportionate verification before reporting. Original task: "
            f"{record.get('task')}"
        )
        capabilities = list(record.get("capabilities") or [])
        job = CodexJob(
            job_id,
            str(record.get("task") or ""),
            str(record.get("workdir") or "."),
            self._command("cloud_deploy" in capabilities),
            model=record.get("model"),
            session_id=record.get("session_id"),
            parent_tool_call_id=record.get("parent_tool_call_id"),
            store=self.store,
            enable_deploy="cloud_deploy" in capabilities,
            deploy_state=record.get("deploy_state"),
            resume_thread_id=str(thread_id),
            recovery_prompt=prompt,
        )
        with self._lock:
            existing = self._jobs.get(job_id)
            if existing is not None and existing.status in _ACTIVE_STATUSES:
                return {"job_id": job_id, "status": existing.status}
            self._jobs[job_id] = job
        self.store.update(
            job_id,
            status="running",
            clear_pending_input=True,
            clear_process=True,
            recovery_count=attempts + 1,
            error="",
        )
        try:
            job.start()
        except Exception as exc:
            with self._lock:
                self._jobs.pop(job_id, None)
            return self._fail_recovery(job_id, f"Failed to restart Codex: {exc}")
        log.info("Recovered Codex job %s on thread %s", job_id, thread_id)
        return {"job_id": job_id, "status": "running", "recovered": True}

    def _fail_recovery(self, job_id: str, message: str) -> dict[str, Any]:
        self.store.update(
            job_id,
            status="error",
            error=message,
            terminal=True,
            clear_process=True,
        )
        self.store.enqueue_signal(job_id, "error")
        self.emit_status(
            job_id,
            "codex.completed",
            {"status": "error", "error": message, "result": None},
        )
        record = self.store.get(job_id)
        if (
            _lifecycle_sink is not None
            and record is not None
            and record.get("session_id")
        ):
            try:
                _lifecycle_sink(str(record["session_id"]), job_id, "error")
            except Exception:
                log.exception("Failed to submit Codex recovery failure continuation")
        log.error("Could not recover Codex job %s: %s", job_id, message)
        return {"job_id": job_id, "status": "error", "error": message}

    @staticmethod
    def _terminate_recorded_process(record: dict[str, Any]) -> bool:
        pid = record.get("process_pid")
        expected = record.get("process_identity")
        if not isinstance(pid, int) or not isinstance(expected, str) or not expected:
            return False
        actual = _process_identity(pid)
        if actual != expected or "codex" not in actual.lower():
            return False
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            return False

    def emit_status(
        self, job_id: str, event_type: str, payload: dict[str, Any]
    ) -> None:
        record = self.store.get(job_id)
        if record is None or not record.get("session_id"):
            return
        sequence = self.store.append_gateway_event(
            job_id,
            str(record["session_id"]),
            event_type,
            {"job_id": job_id, **payload},
        )
        delivered_payload = {
            "job_id": job_id,
            **payload,
            "codex_event_id": f"{job_id}:{sequence}",
        }
        if _progress_sink is not None:
            try:
                _progress_sink(
                    str(record["session_id"]),
                    event_type,
                    delivered_payload,
                )
                self.store.complete_gateway_event(job_id, sequence)
            except Exception:
                log.exception("Failed to publish Codex status event %s", event_type)

    def cleanup(self) -> int:
        return self.store.cleanup(RETENTION_DAYS)

    def metrics(self) -> dict[str, Any]:
        result = self.store.metrics()
        with self._lock:
            result["live_jobs"] = sum(
                1 for job in self._jobs.values() if job.status in _ACTIVE_STATUSES
            )
        return result


_manager = CodexJobManager(CodexRunStore())


def codex_start(
    task: str | None = None,
    path: str = ".",
    model: str | None = None,
    deploy: bool = False,
    fc=None,
) -> dict[str, Any]:
    """Start an interactive Codex coding job in the background."""
    return _manager.start(
        task or "",
        path=path,
        model=model,
        enable_deploy=bool(deploy),
        session_id=get_current_chat_id(),
        parent_run_id=get_current_run_id(),
        parent_tool_call_id=str(getattr(fc, "call_id", None))
        if getattr(fc, "call_id", None)
        else None,
    )


def codex_poll(
    job_id: str | None = None, cursor: int = 0, wait: float = 0.0, fc=None
) -> dict[str, Any]:
    """Poll a job. ``awaiting_input`` includes a structured ``pending_input``."""
    return _manager.poll(
        job_id or "", cursor=cursor, wait=wait, session_id=get_current_chat_id()
    )


def codex_answer(
    job_id: str | None = None, answers: dict[str, Any] | None = None, fc=None
) -> dict[str, Any]:
    """Answer the questions in a Codex job's ``pending_input`` object."""
    return _manager.answer(
        job_id or "", answers or {}, session_id=get_current_chat_id()
    )


def codex_stop(job_id: str | None = None, fc=None) -> dict[str, Any]:
    """Interrupt and stop a Codex job."""
    return _manager.stop(job_id or "", session_id=get_current_chat_id())
