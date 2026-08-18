"""Background Pi coding-agent jobs backed by Pi's RPC mode."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from time import monotonic
from typing import Any, Literal
from uuid import uuid4

from ..context import (
    get_current_chat_files_dir,
    resolve_chat_files_path,
)
from ...workspace import get_workspace_dir
from ..tools.path_security import validate_within_dir
from .protocol import PiRPCClient, PiRPCError, normalize_pi_event

PiProfile = Literal["coding", "read_only"]

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEPLOY_EXTENSION = (
    _REPO_ROOT / "aios_core" / "agent" / "pi" / "extensions" / "deploy.ts"
)
_CLOUD_TOOL_NAMES = (
    "deploy",
    "deployment_status",
    "get_deployment_status",
    "get_deployment_events",
    "get_app_info",
    "check_app_status",
    "cancel_cloud_deployment",
    "resume_cloud_deployment",
    "rollback_cloud_deployment",
    "upload_app_media",
    "list_app_media",
    "get_app_media_url",
    "delete_app_media",
    "list_database_tables",
    "inspect_database_table",
    "query_database_table",
    "list_database_migrations",
)
_ACTIVE_STATUSES = {"starting", "running", "stopping"}
_TERMINAL_STATUSES = {"done", "error", "stopped"}
_ANONYMOUS_OWNER = "default"

SAFETY_CAP_SECONDS = float(os.getenv("AIOS_PI_SAFETY_CAP", "1800"))
MAX_ACTIVE_JOBS = max(1, int(os.getenv("AIOS_PI_MAX_JOBS", "6")))
HANDSHAKE_TIMEOUT_SECONDS = float(os.getenv("AIOS_PI_HANDSHAKE_TIMEOUT", "10"))
RPC_TIMEOUT_SECONDS = float(os.getenv("AIOS_PI_RPC_TIMEOUT", "10"))
ABORT_TIMEOUT_SECONDS = float(os.getenv("AIOS_PI_ABORT_TIMEOUT", "2"))
STOP_GRACE_SECONDS = float(os.getenv("AIOS_PI_STOP_GRACE", "0.5"))
MAX_POLL_WAIT_SECONDS = 30.0
MAX_EVENTS = max(10, int(os.getenv("AIOS_PI_MAX_EVENTS", "500")))
MAX_EVENT_BUFFER_BYTES = max(
    16_384, int(os.getenv("AIOS_PI_MAX_EVENT_BUFFER_BYTES", "1048576"))
)
MAX_POLL_EVENTS = max(1, int(os.getenv("AIOS_PI_MAX_POLL_EVENTS", "100")))
MAX_POLL_BYTES = max(1024, int(os.getenv("AIOS_PI_MAX_POLL_BYTES", "65536")))
TOOL_UPDATE_COALESCE_SECONDS = max(
    0.01, float(os.getenv("AIOS_PI_TOOL_UPDATE_INTERVAL", "0.25"))
)
MAX_STDERR_BYTES = max(1024, int(os.getenv("AIOS_PI_MAX_STDERR_BYTES", "65536")))
MAX_RESULT_CHARS = max(1024, int(os.getenv("AIOS_PI_MAX_RESULT_CHARS", "100000")))
FINISHED_JOB_TTL_SECONDS = max(60.0, float(os.getenv("AIOS_PI_JOB_TTL", "3600")))
MAX_JOB_RECORDS = max(MAX_ACTIVE_JOBS, int(os.getenv("AIOS_PI_MAX_RECORDS", "100")))

_ProgressSink = Callable[[str, str, dict[str, Any]], None]
_progress_sink: _ProgressSink | None = None


def set_progress_sink(sink: _ProgressSink | None) -> None:
    """Install the server's optional, best-effort live progress publisher."""
    global _progress_sink
    _progress_sink = sink


# Only runtime essentials and documented model-provider credentials are passed
# through.  In particular, arbitrary application/database secrets are not
# inherited by Pi's shell tool.
_ENV_ALLOWLIST = {
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "TERM",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "NODE_EXTRA_CA_CERTS",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_OAUTH_TOKEN",
    "ANT_LING_API_KEY",
    "OPENAI_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_BASE_URL",
    "AZURE_OPENAI_RESOURCE_NAME",
    "AZURE_OPENAI_API_VERSION",
    "AZURE_OPENAI_DEPLOYMENT_NAME_MAP",
    "DEEPSEEK_API_KEY",
    "NVIDIA_API_KEY",
    "GEMINI_API_KEY",
    "GROQ_API_KEY",
    "CEREBRAS_API_KEY",
    "XAI_API_KEY",
    "FIREWORKS_API_KEY",
    "TOGETHER_API_KEY",
    "BASETEN_API_KEY",
    "OPENROUTER_API_KEY",
    "AI_GATEWAY_API_KEY",
    "ZAI_API_KEY",
    "ZAI_CODING_CN_API_KEY",
    "MISTRAL_API_KEY",
    "MINIMAX_API_KEY",
    "MOONSHOT_API_KEY",
    "OPENCODE_API_KEY",
    "KIMI_API_KEY",
    "CLOUDFLARE_API_KEY",
    "CLOUDFLARE_ACCOUNT_ID",
    "CLOUDFLARE_GATEWAY_ID",
    "QWEN_TOKEN_PLAN_API_KEY",
    "QWEN_TOKEN_PLAN_CN_API_KEY",
    "XIAOMI_API_KEY",
    "XIAOMI_TOKEN_PLAN_CN_API_KEY",
    "XIAOMI_TOKEN_PLAN_AMS_API_KEY",
    "XIAOMI_TOKEN_PLAN_SGP_API_KEY",
    "AWS_PROFILE",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_BEARER_TOKEN_BEDROCK",
    "AWS_REGION",
    "PI_CODING_AGENT_DIR",
    "PI_PACKAGE_DIR",
    "PI_OFFLINE",
    # The deploy bridge must select the same ProjectStore root as its parent.
    "AIOS_ENV",
    "APP_ENV",
    "ENV",
    "AIOS_DATA_DIR",
    "AIOS_CLOUD_URL",
    # This device-scoped control-plane credential is required only by the
    # trusted deploy extension. Application/database secrets remain excluded.
    "AIOS_CLOUD_DEVICE_TOKEN",
}


def sanitized_pi_environment(source: dict[str, str] | None = None) -> dict[str, str]:
    """Build the deliberately small environment inherited by a Pi worker."""
    source = os.environ if source is None else source
    env = {
        key: str(source[key]) for key in _ENV_ALLOWLIST if source.get(key) is not None
    }
    env.setdefault("PATH", os.defpath)
    env.setdefault("LANG", "C.UTF-8")
    env.setdefault("TERM", "dumb")
    env["PI_TELEMETRY"] = "0"
    # The trusted deploy extension invokes the current Python runtime and needs
    # to import this checkout, not whichever mini-aios happens to be installed.
    env["AIOS_PYTHON"] = sys.executable
    env["PYTHONPATH"] = str(_REPO_ROOT)
    return env


def build_pi_command(
    *,
    profile: PiProfile = "coding",
    provider: str | None = None,
    model: str | None = None,
    thinking_level: str | None = None,
    deploy_extension: Path | None = None,
) -> list[str]:
    """Build a deterministic headless Pi command with discovery disabled."""
    if profile not in {"coding", "read_only"}:
        raise ValueError("profile must be 'coding' or 'read_only'")
    extension = _DEPLOY_EXTENSION if deploy_extension is None else deploy_extension
    command = [
        "pi",
        "--mode",
        "rpc",
        "--no-session",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-themes",
        "--no-context-files",
        "--no-approve",
    ]
    tools = ["read", "grep", "find", "ls"]
    if profile == "coding":
        tools.extend(["bash", "edit", "write"])
        if extension.is_file():
            command.extend(["--extension", str(extension.resolve())])
            tools.extend(_CLOUD_TOOL_NAMES)
    command.extend(["--tools", ",".join(tools)])
    if isinstance(provider, str) and provider.strip():
        command.extend(["--provider", provider.strip()])
    if isinstance(model, str) and model.strip():
        command.extend(["--model", model.strip()])
    if isinstance(thinking_level, str) and thinking_level.strip():
        command.extend(["--thinking", thinking_level.strip()])
    return command


def _allowed_roots() -> list[Path]:
    roots: list[Path] = []
    chat_root = get_current_chat_files_dir()
    if chat_root is not None:
        roots.append(chat_root)
    roots.append(get_workspace_dir())
    configured = os.getenv("AIOS_PI_ALLOWED_ROOTS", "")
    for raw_root in configured.split(os.pathsep):
        if raw_root.strip():
            roots.append(Path(raw_root.strip()).expanduser())
    # Preserve order while avoiding duplicate diagnostics.
    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root.resolve())
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return unique


def resolve_pi_workdir(path: str) -> Path:
    """Resolve a workdir and reject traversal/symlink escapes from allowed roots."""
    if not isinstance(path, str) or not path.strip():
        raise ValueError("path must be a non-empty string")
    candidate = resolve_chat_files_path(path.strip())
    if not candidate.exists():
        raise ValueError(f"path does not exist: {candidate}")
    if not candidate.is_dir():
        raise ValueError(f"path is not a directory: {candidate}")
    resolved = candidate.resolve()
    roots = _allowed_roots()
    if not any(validate_within_dir(resolved, root) is None for root in roots):
        allowed = ", ".join(str(root.resolve()) for root in roots)
        raise ValueError(f"path escapes allowed Pi roots ({allowed}): {resolved}")
    return resolved


def _extract_assistant_text(message: Any) -> str | None:
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    chunks: list[str] = []
    for block in content:
        if (
            isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ):
            chunks.append(block["text"])
    return "".join(chunks)


def _poll_event(event: dict[str, Any], event_bytes: int) -> tuple[dict[str, Any], int]:
    """Return an event representation that fits the configured page budget."""
    if event_bytes <= MAX_POLL_BYTES:
        return event, event_bytes
    compact = {
        key: event[key]
        for key in ("kind", "tool_call_id", "tool_name", "is_error")
        if key in event
    }
    compact["detail"] = (
        f"event payload omitted ({event_bytes} bytes exceeds "
        f"{MAX_POLL_BYTES} byte poll-page limit)"
    )
    compact_bytes = len(
        json.dumps(compact, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    return compact, compact_bytes


class PiJob:
    """One Pi RPC process.  All mutable public state is guarded by ``_lock``."""

    def __init__(
        self,
        job_id: str,
        task: str,
        workdir: str,
        command: list[str],
        *,
        owner_session_id: str,
        progress_session_id: str | None = None,
        parent_tool_call_id: str | None = None,
        environment: dict[str, str] | None = None,
        safety_cap: float = SAFETY_CAP_SECONDS,
        max_events: int = MAX_EVENTS,
    ) -> None:
        self.id = job_id
        self.task = task
        self.workdir = workdir
        self.command = command
        self.owner_session_id = owner_session_id
        self.progress_session_id = progress_session_id
        self.parent_tool_call_id = parent_tool_call_id
        self.environment = environment or sanitized_pi_environment()
        self.safety_cap = max(0.01, float(safety_cap))
        self.max_events = max(1, int(max_events))

        self.status = "starting"
        self.error: str | None = None
        self.result: str | None = None
        self.stats: dict[str, Any] | None = None
        self.started_at = monotonic()
        self.finished_at: float | None = None

        # Explicit sequence numbers let a noisy accumulated tool update replace
        # its previous snapshot without breaking absolute cursor semantics.
        self._events: list[tuple[int, dict[str, Any], int]] = []
        self._next_event_cursor = 0
        self._event_bytes = 0
        self._tool_update_state: dict[str, tuple[float, int]] = {}
        self._stderr = bytearray()
        self._last_assistant_text = ""
        self._result_truncated = False
        self._assistant_stop_reason: str | None = None
        self._assistant_error_message: str | None = None
        self._proc: subprocess.Popen[bytes] | None = None
        self._rpc: PiRPCClient | None = None
        self._lock = threading.RLock()
        self._new_event = threading.Event()
        self._finished = threading.Event()
        self._settle_finalizer_started = False
        self._settled_pending_acceptance = False
        self._intentional_shutdown = False
        self._shutdown_started = False
        self._stop_requested = False
        self._prompt_accepted = False
        self._completion_emitted = False

    def start(self) -> None:
        """Spawn Pi and complete get_state + prompt-acceptance handshakes."""
        proc = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.workdir,
            env=self.environment,
            text=False,
            bufsize=0,
            start_new_session=True,
            close_fds=True,
        )
        if proc.stdin is None or proc.stdout is None or proc.stderr is None:
            self._terminate_process(proc)
            raise RuntimeError("Pi RPC pipes were not created")
        self._proc = proc
        rpc = PiRPCClient(
            proc.stdin,
            proc.stdout,
            on_event=self._on_rpc_event,
            on_close=self._on_rpc_close,
        )
        self._rpc = rpc
        threading.Thread(
            target=self._drain_stderr,
            name=f"pi-stderr-{self.id}",
            daemon=True,
        ).start()
        threading.Thread(
            target=self._watch_process,
            name=f"pi-process-{self.id}",
            daemon=True,
        ).start()
        threading.Thread(
            target=self._watchdog,
            name=f"pi-watchdog-{self.id}",
            daemon=True,
        ).start()
        rpc.start(name=f"pi-rpc-{self.id}")
        try:
            state_response = rpc.request("get_state", timeout=HANDSHAKE_TIMEOUT_SECONDS)
            state = state_response.get("data")
            if not isinstance(state, dict):
                raise PiRPCError("Pi get_state returned no state data")
            rpc.request("prompt", timeout=HANDSHAKE_TIMEOUT_SECONDS, message=self.task)
        except Exception as exc:
            detail = (
                self._stderr_text() or str(exc) or "Pi RPC startup handshake failed"
            )
            self._finish("error", error=detail)
            self._shutdown_process()
            raise

        status, error, should_finalize = self._mark_prompt_accepted()
        if should_finalize:
            self._launch_settle_finalizer()
        if status in _TERMINAL_STATUSES and status != "done":
            raise RuntimeError(error or "Pi stopped during startup")

    def is_active(self) -> bool:
        with self._lock:
            return self.status in _ACTIVE_STATUSES

    def _mark_prompt_accepted(self) -> tuple[str, str | None, bool]:
        with self._lock:
            # Keep acceptance, started publication, and terminal-race repair in
            # one critical section.  A concurrent crash can therefore publish
            # neither completion-before-started nor an orphan started event.
            self._prompt_accepted = True
            if self.status == "starting":
                self.status = "running"
            self._emit(
                "pi.started", {"task_summary": self.task[:200], "workdir": self.workdir}
            )
            self._emit_completion_once_locked()
            should_finalize = (
                self._settled_pending_acceptance
                and not self._stop_requested
                and not self._settle_finalizer_started
                and self.status in _ACTIVE_STATUSES
            )
            if should_finalize:
                self._settle_finalizer_started = True
            return self.status, self.error, should_finalize

    def poll(self, cursor: int = 0, wait: float = 0.0) -> dict[str, Any]:
        try:
            requested_cursor = max(0, int(cursor or 0))
        except (TypeError, ValueError):
            requested_cursor = 0
        bounded_wait = min(max(float(wait or 0.0), 0.0), MAX_POLL_WAIT_SECONDS)
        if bounded_wait:
            deadline = monotonic() + bounded_wait
            while monotonic() < deadline:
                with self._lock:
                    ready = (
                        any(
                            sequence >= requested_cursor
                            for sequence, _, _ in self._events
                        )
                        or self.status in _TERMINAL_STATUSES
                    )
                if ready:
                    break
                self._new_event.wait(
                    timeout=min(0.25, max(0.0, deadline - monotonic()))
                )
                self._new_event.clear()
        with self._lock:
            latest_cursor = self._next_event_cursor
            buffer_start = self._events[0][0] if self._events else latest_cursor
            normalized_cursor = min(max(requested_cursor, buffer_start), latest_cursor)
            available = [
                record for record in self._events if record[0] >= normalized_cursor
            ]
            page: list[dict[str, Any]] = []
            page_bytes = 0
            page_cursor = latest_cursor
            for sequence, event, event_bytes in available:
                rendered_event, rendered_bytes = _poll_event(event, event_bytes)
                if page and (
                    len(page) >= MAX_POLL_EVENTS
                    or page_bytes + rendered_bytes > MAX_POLL_BYTES
                ):
                    page_cursor = sequence
                    break
                page.append(rendered_event)
                page_bytes += rendered_bytes
                page_cursor = sequence + 1
            has_more = any(sequence >= page_cursor for sequence, _, _ in available)
            return {
                "job_id": self.id,
                "status": self.status,
                "events": list(page),
                "cursor": page_cursor,
                "next_cursor": page_cursor,
                "latest_cursor": latest_cursor,
                "has_more": has_more,
                "buffer_start_cursor": buffer_start,
                "cursor_reset": requested_cursor < buffer_start,
                "result": self.result if self.status == "done" else None,
                "result_truncated": self._result_truncated
                if self.status == "done"
                else False,
                "stats": dict(self.stats) if self.stats is not None else None,
                "error": self.error,
            }

    def steer(self, message: str) -> dict[str, Any]:
        if not isinstance(message, str) or not message.strip():
            return {"error": "message is required"}
        with self._lock:
            if self.status != "running":
                return {"error": f"Pi job is not running: {self.id} ({self.status})"}
            rpc = self._rpc
        if rpc is None:
            return {"error": f"Pi RPC is unavailable: {self.id}"}
        try:
            rpc.request("steer", timeout=RPC_TIMEOUT_SECONDS, message=message.strip())
        except PiRPCError as exc:
            return {
                "error": str(exc),
                "job_id": self.id,
                "status": self.current_status(),
            }
        return {"job_id": self.id, "status": self.current_status(), "accepted": True}

    def stop(self, *, reason: str = "stopped by request") -> None:
        with self._lock:
            if self.status in _TERMINAL_STATUSES:
                return
            self._stop_requested = True
            self.status = "stopping"
            rpc = self._rpc
        if rpc is not None and not rpc.closed:
            try:
                rpc.request("abort", timeout=ABORT_TIMEOUT_SECONDS)
            except PiRPCError:
                pass
        # Give Pi a short opportunity to settle after abort, then always close
        # the entire process group so bash descendants cannot outlive the job.
        self._finished.wait(timeout=max(0.0, STOP_GRACE_SECONDS))
        self._finish("stopped", error=reason)
        self._shutdown_process()

    def summary(self) -> dict[str, Any]:
        with self._lock:
            return {
                "job_id": self.id,
                "status": self.status,
                "task": self.task[:80],
                "workdir": self.workdir,
                "events": self._next_event_cursor,
            }

    def current_status(self) -> str:
        with self._lock:
            return self.status

    def _on_rpc_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if event_type == "extension_ui_request":
            # Headless workers never grant interactive extension requests.  A
            # cancelled response prevents an extension from waiting forever.
            rpc = self._rpc
            request_id = event.get("id")
            if rpc is not None and request_id is not None:
                try:
                    rpc.send(
                        {
                            "type": "extension_ui_response",
                            "id": str(request_id),
                            "cancelled": True,
                        }
                    )
                except PiRPCError:
                    pass
            self._append_event(
                {
                    "kind": "extension_ui_cancelled",
                    "method": event.get("method"),
                }
            )
            return
        if event_type == "message_start":
            message = event.get("message")
            if isinstance(message, dict) and message.get("role") == "assistant":
                with self._lock:
                    self._last_assistant_text = ""
                    self._result_truncated = False
                    self._assistant_stop_reason = None
                    self._assistant_error_message = None
        elif event_type == "message_update":
            update = event.get("assistantMessageEvent")
            if isinstance(update, dict) and update.get("type") == "text_delta":
                delta = update.get("delta")
                if isinstance(delta, str):
                    self._append_assistant_text(delta)
        elif event_type == "message_end":
            message = event.get("message")
            text = _extract_assistant_text(message)
            if text is not None:
                self._set_assistant_text(text)
            if isinstance(message, dict) and message.get("role") == "assistant":
                with self._lock:
                    stop_reason = message.get("stopReason")
                    error_message = message.get("errorMessage")
                    self._assistant_stop_reason = (
                        str(stop_reason) if stop_reason is not None else None
                    )
                    self._assistant_error_message = (
                        str(error_message) if error_message is not None else None
                    )

        normalized = normalize_pi_event(event)
        if normalized is not None:
            should_emit = self._append_event(normalized)
            if should_emit and normalized["kind"] not in {
                "agent_start",
                "agent_settled",
                "turn_start",
                "turn_end",
            }:
                self._emit("pi.progress", normalized)

        if event_type == "agent_settled":
            with self._lock:
                if self._stop_requested:
                    return
                if (
                    self.status not in _ACTIVE_STATUSES
                    or self._settle_finalizer_started
                ):
                    return
                if not self._prompt_accepted:
                    self._settled_pending_acceptance = True
                    return
                self._settle_finalizer_started = True
            self._launch_settle_finalizer()

    def _launch_settle_finalizer(self) -> None:
        # A request from the RPC reader callback would deadlock waiting for the
        # same reader.  Finalize on a separate thread.
        threading.Thread(
            target=self._finalize_settled,
            name=f"pi-finalize-{self.id}",
            daemon=True,
        ).start()

    def _finalize_settled(self) -> None:
        rpc = self._rpc
        text: str | None = None
        stats: dict[str, Any] | None = None
        if rpc is not None and not rpc.closed:
            try:
                response = rpc.request(
                    "get_last_assistant_text", timeout=RPC_TIMEOUT_SECONDS
                )
                data = response.get("data")
                if isinstance(data, dict) and isinstance(data.get("text"), str):
                    text = data["text"]
            except PiRPCError:
                pass
            try:
                response = rpc.request("get_session_stats", timeout=RPC_TIMEOUT_SECONDS)
                data = response.get("data")
                if isinstance(data, dict):
                    stats = data
            except PiRPCError:
                pass
        if text is not None:
            self._set_assistant_text(text)
        with self._lock:
            result = self._last_assistant_text.strip() or "(empty)"
            stop_reason = (self._assistant_stop_reason or "").strip().lower()
            assistant_error = (self._assistant_error_message or "").strip()
            if stats is not None:
                self.stats = stats
        if stop_reason in {"error", "aborted"}:
            detail = assistant_error or f"Pi assistant stopped with {stop_reason}"
            self._finish("error", error=detail)
        else:
            self._finish("done", result=result)
        self._shutdown_process()

    def _on_rpc_close(self, protocol_error: str | None) -> None:
        with self._lock:
            if self._intentional_shutdown or self.status in _TERMINAL_STATUSES:
                return
        detail = (
            protocol_error
            or self._stderr_text()
            or "Pi RPC stream closed before agent_settled"
        )
        self._finish("error", error=detail)
        self._shutdown_process()

    def _watch_process(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            returncode = proc.wait()
        except Exception as exc:
            with self._lock:
                terminal = self.status in _TERMINAL_STATUSES
            if not terminal:
                self._finish("error", error=f"failed waiting for Pi process: {exc}")
                self._shutdown_process()
            return
        with self._lock:
            if self._intentional_shutdown or self.status in _TERMINAL_STATUSES:
                return
        stderr = self._stderr_text()
        detail = stderr or f"Pi exited {returncode} before agent_settled"
        self._finish("error", error=detail)
        self._shutdown_process()

    def _watchdog(self) -> None:
        if self._finished.wait(timeout=self.safety_cap):
            return
        self._finish("error", error=f"Pi exceeded safety cap {self.safety_cap:g}s")
        self._shutdown_process()

    def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            while True:
                chunk = proc.stderr.read(4096)
                if not chunk:
                    return
                if isinstance(chunk, str):  # tolerate simple test doubles
                    chunk = chunk.encode("utf-8", errors="replace")
                with self._lock:
                    self._stderr.extend(chunk)
                    if len(self._stderr) > MAX_STDERR_BYTES:
                        del self._stderr[: len(self._stderr) - MAX_STDERR_BYTES]
        except Exception:
            return

    def _append_event(self, event: dict[str, Any]) -> bool:
        """Store a bounded event and return whether live progress should emit.

        Pi tool updates are accumulated snapshots and can arrive for every
        output chunk.  Within the throttle interval we replace the prior
        snapshot for that tool with a newer sequence number.  Poll cursors stay
        absolute while memory and gateway traffic remain bounded.
        """
        try:
            event_bytes = len(
                json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode(
                    "utf-8"
                )
            )
        except (
            TypeError,
            ValueError,
        ):  # pragma: no cover - normalized events are JSON-safe
            event_bytes = len(str(event).encode("utf-8", errors="replace"))
        should_emit = True
        with self._lock:
            kind = event.get("kind")
            tool_call_id = event.get("tool_call_id")
            key = str(tool_call_id) if tool_call_id is not None else ""
            now = monotonic()
            if kind == "tool_update" and key:
                previous = self._tool_update_state.get(key)
                if (
                    previous is not None
                    and now - previous[0] < TOOL_UPDATE_COALESCE_SECONDS
                ):
                    previous_sequence = previous[1]
                    for index, (sequence, _, stored_bytes) in enumerate(self._events):
                        if sequence == previous_sequence:
                            self._event_bytes -= stored_bytes
                            del self._events[index]
                            should_emit = False
                            break

            sequence = self._next_event_cursor
            self._next_event_cursor += 1
            self._events.append((sequence, event, event_bytes))
            self._event_bytes += event_bytes

            if kind == "tool_update" and key:
                prior = self._tool_update_state.get(key)
                last_emitted_at = (
                    prior[0] if prior is not None and not should_emit else now
                )
                self._tool_update_state[key] = (last_emitted_at, sequence)
            elif kind == "tool_end" and key:
                self._tool_update_state.pop(key, None)

            while len(self._events) > 1 and (
                len(self._events) > self.max_events
                or self._event_bytes > MAX_EVENT_BUFFER_BYTES
            ):
                removed_sequence, removed_event, removed_bytes = self._events.pop(0)
                self._event_bytes -= removed_bytes
                removed_id = removed_event.get("tool_call_id")
                if (
                    removed_event.get("kind") == "tool_update"
                    and removed_id is not None
                ):
                    removed_key = str(removed_id)
                    state = self._tool_update_state.get(removed_key)
                    if state is not None and state[1] == removed_sequence:
                        self._tool_update_state.pop(removed_key, None)
        self._new_event.set()
        return should_emit

    def _append_assistant_text(self, text: str) -> None:
        with self._lock:
            combined = self._last_assistant_text + text
            if len(combined) > MAX_RESULT_CHARS:
                combined = combined[-MAX_RESULT_CHARS:]
                self._result_truncated = True
            self._last_assistant_text = combined

    def _set_assistant_text(self, text: str) -> None:
        with self._lock:
            if len(text) > MAX_RESULT_CHARS:
                self._last_assistant_text = text[-MAX_RESULT_CHARS:]
                self._result_truncated = True
            else:
                self._last_assistant_text = text
                self._result_truncated = False

    def _finish(
        self,
        status: Literal["done", "error", "stopped"],
        *,
        error: str | None = None,
        result: str | None = None,
    ) -> None:
        with self._lock:
            if self.status in _TERMINAL_STATUSES:
                return
            self.status = status
            self.error = error
            self.result = result
            self.finished_at = monotonic()
            # Publish before releasing the state lock so observing a terminal
            # status also guarantees the terminal gateway event was emitted.
            self._emit_completion_once_locked()
        self._finished.set()
        self._new_event.set()

    def _emit_completion_once_locked(self) -> None:
        if (
            not self._prompt_accepted
            or self.status not in _TERMINAL_STATUSES
            or self._completion_emitted
        ):
            return
        self._completion_emitted = True
        self._emit(
            "pi.completed",
            {"status": self.status, "result": self.result, "error": self.error},
        )

    def _stderr_text(self) -> str:
        with self._lock:
            return bytes(self._stderr).decode("utf-8", errors="replace").strip()

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        sink = _progress_sink
        with self._lock:
            accepted = self._prompt_accepted
        if not accepted and event_type != "pi.started":
            return
        if sink is None or not self.progress_session_id:
            return
        try:
            sink(
                self.progress_session_id,
                event_type,
                {
                    "job_id": self.id,
                    "parent_tool_call_id": self.parent_tool_call_id,
                    **payload,
                },
            )
        except Exception:
            pass

    def _shutdown_process(self) -> None:
        with self._lock:
            if self._shutdown_started:
                return
            self._shutdown_started = True
            self._intentional_shutdown = True
            rpc = self._rpc
            proc = self._proc
        if rpc is not None:
            rpc.close()
        if proc is not None:
            self._terminate_process(proc)

    @staticmethod
    def _terminate_process(proc: subprocess.Popen[bytes]) -> None:
        if proc.poll() is not None:
            # The Pi leader may have exited while a bash descendant in its
            # dedicated session is still alive.  Its process-group id is the
            # original leader pid because start_new_session=True.
            pid = getattr(proc, "pid", None)
            if isinstance(pid, int) and pid > 0 and hasattr(os, "killpg"):
                try:
                    os.killpg(pid, signal.SIGTERM)
                    os.killpg(pid, signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass
            return
        PiJob._signal_process(proc, signal.SIGTERM)
        try:
            proc.wait(timeout=max(0.0, STOP_GRACE_SECONDS))
        except subprocess.TimeoutExpired:
            pass
        if proc.poll() is None:
            PiJob._signal_process(proc, signal.SIGKILL)
            try:
                proc.wait(timeout=max(0.0, STOP_GRACE_SECONDS))
            except subprocess.TimeoutExpired:
                pass

    @staticmethod
    def _signal_process(proc: subprocess.Popen[bytes], sig: signal.Signals) -> None:
        pid = getattr(proc, "pid", None)
        if isinstance(pid, int) and pid > 0 and hasattr(os, "killpg"):
            try:
                # start_new_session=True makes the Pi pid its process-group id.
                os.killpg(pid, sig)
                return
            except (OSError, ProcessLookupError):
                pass
        try:
            if sig == signal.SIGTERM:
                proc.terminate()
            elif sig == signal.SIGKILL:
                proc.kill()
            else:
                proc.send_signal(sig)
        except (OSError, ProcessLookupError):
            pass


class PiJobManager:
    """Own Pi jobs and enforce a concurrency cap and per-chat access."""

    def __init__(self, *, max_active_jobs: int = MAX_ACTIVE_JOBS) -> None:
        self._jobs: dict[str, PiJob] = {}
        self._lock = threading.Lock()
        self._max_active_jobs = max(1, int(max_active_jobs))

    def _prune_locked(self, *, reserve: int = 0) -> None:
        """Bound retained terminal metadata without ever discarding live jobs."""
        now = monotonic()
        terminal: list[tuple[float, str]] = []
        for job_id, job in self._jobs.items():
            with job._lock:
                status = job.status
                finished_at = job.finished_at
            if status in _TERMINAL_STATUSES:
                terminal.append((finished_at or job.started_at, job_id))
        for finished_at, job_id in terminal:
            if now - finished_at > FINISHED_JOB_TTL_SECONDS:
                self._jobs.pop(job_id, None)
        terminal = sorted(
            (finished_at, job_id)
            for finished_at, job_id in terminal
            if job_id in self._jobs
        )
        excess = max(0, len(self._jobs) + reserve - MAX_JOB_RECORDS)
        for _, job_id in terminal[:excess]:
            self._jobs.pop(job_id, None)

    @staticmethod
    def _owner(session_id: str | None) -> str:
        return session_id or _ANONYMOUS_OWNER

    def start(
        self,
        task: str,
        *,
        path: str = ".",
        model: str | None = None,
        provider: str | None = None,
        thinking_level: str | None = None,
        profile: PiProfile = "coding",
        session_id: str | None = None,
        parent_tool_call_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(task, str) or not task.strip():
            return {"error": "task is required"}
        try:
            workdir = resolve_pi_workdir(path)
            command = build_pi_command(
                profile=profile,
                model=model,
                provider=provider,
                thinking_level=thinking_level,
            )
        except ValueError as exc:
            return {"error": str(exc)}

        owner = self._owner(session_id)
        job_id = uuid4().hex[:12]
        job = PiJob(
            job_id,
            task.strip(),
            str(workdir),
            command,
            owner_session_id=owner,
            progress_session_id=session_id,
            parent_tool_call_id=parent_tool_call_id,
        )
        # Reserve the slot and insert the job in one critical section.  Two
        # concurrent starts can therefore never both pass the cap.
        with self._lock:
            self._prune_locked(reserve=1)
            active = [job for job in self._jobs.values() if job.is_active()]
            if len(active) >= self._max_active_jobs:
                running = [job.id for job in active]
                return {
                    "error": f"too many active Pi jobs ({self._max_active_jobs}); running: {running}"
                }
            self._jobs[job_id] = job
        try:
            job.start()
        except FileNotFoundError:
            with self._lock:
                self._jobs.pop(job_id, None)
            return {"error": "Pi CLI is not installed or not on PATH"}
        except Exception as exc:
            with self._lock:
                self._jobs.pop(job_id, None)
            return {"error": f"failed to start Pi -- {exc}"}
        return {
            "job_id": job_id,
            "status": job.current_status(),
            "workdir": str(workdir),
            "profile": profile,
        }

    def poll(
        self,
        job_id: str,
        *,
        cursor: int = 0,
        wait: float = 0.0,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        job_or_error = self._get_owned(job_id, session_id)
        if isinstance(job_or_error, dict):
            return job_or_error
        return job_or_error.poll(cursor=cursor, wait=wait)

    def steer(
        self, job_id: str, message: str, *, session_id: str | None = None
    ) -> dict[str, Any]:
        job_or_error = self._get_owned(job_id, session_id)
        if isinstance(job_or_error, dict):
            return job_or_error
        return job_or_error.steer(message)

    def stop(self, job_id: str, *, session_id: str | None = None) -> dict[str, Any]:
        job_or_error = self._get_owned(job_id, session_id)
        if isinstance(job_or_error, dict):
            return job_or_error
        job_or_error.stop()
        return {"job_id": job_id, "status": job_or_error.current_status()}

    def list(self, *, session_id: str | None = None) -> dict[str, Any]:
        owner = self._owner(session_id)
        with self._lock:
            self._prune_locked()
            jobs = [
                job.summary()
                for job in self._jobs.values()
                if job.owner_session_id == owner
            ]
        return {"jobs": jobs}

    def close_all(self) -> None:
        with self._lock:
            jobs = list(self._jobs.values())
            self._jobs.clear()
        for job in jobs:
            job.stop(reason="stopped during runtime shutdown")

    def _get_owned(self, job_id: str, session_id: str | None) -> PiJob | dict[str, str]:
        if not isinstance(job_id, str) or not job_id.strip():
            return {"error": "job_id is required"}
        with self._lock:
            self._prune_locked()
            job = self._jobs.get(job_id.strip())
        if job is None:
            return {"error": f"unknown job_id: {job_id}"}
        if job.owner_session_id != self._owner(session_id):
            return {"error": f"job_id is not owned by this chat: {job_id}"}
        return job


_manager = PiJobManager()


def get_pi_job_manager() -> PiJobManager:
    return _manager


def close_all_pi_jobs() -> None:
    """Stop all Pi process groups during runtime shutdown."""
    _manager.close_all()
