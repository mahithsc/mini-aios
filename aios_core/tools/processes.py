from __future__ import annotations

import errno
import os
import pty
import re
import select
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ..apps.paths import (
    AppHostExecutionDenied,
    ensure_host_execution_allowed,
    protected_app_roots,
)
from ..runtime_context import default_chat_files_cwd, resolve_chat_files_path
from ..workspace import PathAccessError
from .ansi_strip import strip_ansi
from .execution_sandbox import sandboxed_command

_DEFAULT_SHELL = "/bin/bash"
_DEFAULT_OUTPUT_LIMIT = 200_000
_READ_CHUNK_SIZE = 4096
_DONE_MARKER_RE = re.compile(r"__AIOS_CMD_DONE__:([a-f0-9]+):(-?\d+)")
_MAX_SESSIONS = 8
_MAX_POLL_WAIT = 30.0
# Internal command-wrapper lines (markers + their echoed printf/unset
# commands) are protocol, not terminal output — never show them to the model.
_MARKER_SCRUB_RE = re.compile(
    r"^.*(?:__AIOS_CMD_(?:START|DONE)__|__aios_exit_code).*(?:\r?\n)?",
    re.MULTILINE,
)
_ALLOWED_SIGNALS = {
    "SIGTERM", "SIGKILL", "SIGINT", "SIGHUP", "SIGQUIT",
    "SIGUSR1", "SIGUSR2", "SIGSTOP", "SIGCONT",
}


def _scrub_output(text: str) -> str:
    return strip_ansi(_MARKER_SCRUB_RE.sub("", text))


def _normalize_env(env: dict[str, str] | None) -> dict[str, str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update({key: str(value) for key, value in env.items()})

    # Keep the interactive shell deterministic and quiet.
    merged_env.setdefault("PS1", "")
    merged_env.setdefault("PROMPT_COMMAND", "")
    merged_env.setdefault("TERM", "xterm-256color")
    merged_env.setdefault("HISTFILE", "")
    merged_env.setdefault("BASH_SILENCE_DEPRECATION_WARNING", "1")
    return merged_env


@dataclass
class ProcessSession:
    cwd: str
    env: dict[str, str]
    shell: str = _DEFAULT_SHELL
    output_limit: int = _DEFAULT_OUTPUT_LIMIT
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)
    last_activity_at: float = field(default_factory=time.time)
    master_fd: int | None = None
    proc: subprocess.Popen | None = None
    status: str = "idle"
    buffer: str = ""
    buffer_start_cursor: int = 0
    reader_thread: threading.Thread | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    active_command: dict[str, object] | None = None
    scan_pos: int = 0
    protected_roots: tuple[str, ...] = ()

    def start(self) -> ProcessSession:
        master_fd, slave_fd = pty.openpty()
        proc = None
        try:
            proc = subprocess.Popen(
                sandboxed_command(
                    [self.shell, "--noprofile", "--norc"],
                    protected_roots=tuple(Path(root) for root in self.protected_roots),
                ),
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                cwd=self.cwd,
                env=self.env,
                start_new_session=True,
                close_fds=True,
            )
        except Exception:
            os.close(master_fd)
            raise
        finally:
            os.close(slave_fd)

        os.set_blocking(master_fd, False)
        self.master_fd = master_fd
        self.proc = proc
        self.status = "idle"

        self.reader_thread = threading.Thread(
            target=self._reader_loop,
            name=f"pty-session-{self.id[:8]}",
            daemon=True,
        )
        self.reader_thread.start()
        return self

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc is not None else None

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def summary(self) -> dict[str, object]:
        return {
            "process_id": self.id,
            "pid": self.pid,
            "status": self._current_status(),
            "cwd": self.cwd,
            "shell": self.shell,
            "created_at": self.created_at,
            "last_activity_at": self.last_activity_at,
            "shell_alive": self.is_alive(),
            "command": self._command_payload(),
        }

    def send_command(self, command: str) -> dict[str, object]:
        stripped_command = (command or "").strip()
        if not stripped_command:
            return {"error": "command is required"}
        with self.lock:
            if self.status == "exited":
                return {"error": f"process has exited: {self.id}"}
            if self.active_command and self.active_command.get("status") == "running":
                return {"error": f"process already has an active command: {self.id}"}

            command_id = uuid.uuid4().hex
            self.active_command = {
                "id": command_id,
                "status": "running",
                "exit_code": None,
                "command": stripped_command,
                "started_at": time.time(),
            }
            self.status = "running"

        wrapped_command = (
            f"printf '__AIOS_CMD_START__:{command_id}\\n'\n"
            f"{stripped_command}\n"
            "__aios_exit_code=$?\n"
            f"printf '\\n__AIOS_CMD_DONE__:{command_id}:%s\\n' \"$__aios_exit_code\"\n"
            "unset __aios_exit_code\n"
        )
        self._write(wrapped_command)
        return {
            "process_id": self.id,
            "status": self._current_status(),
            "command": self._command_payload(),
        }

    def send_input(self, input_text: str) -> dict[str, object]:
        if input_text is None:
            return {"error": "input is required"}
        with self.lock:
            if self.status == "exited":
                return {"error": f"process has exited: {self.id}"}
        self._write(input_text)
        return {
            "process_id": self.id,
            "status": self._current_status(),
            "command": self._command_payload(),
        }

    def poll(self, cursor: int = 0) -> dict[str, object]:
        with self.lock:
            current_cursor = self.buffer_start_cursor + len(self.buffer)
            normalized_cursor = max(int(cursor or 0), self.buffer_start_cursor)
            offset = normalized_cursor - self.buffer_start_cursor
            output = _scrub_output(self.buffer[offset:])
            return {
                "process_id": self.id,
                "pid": self.pid,
                "status": self._current_status(),
                "cwd": self.cwd,
                "shell": self.shell,
                "shell_alive": self.is_alive(),
                "output": output,
                "next_cursor": current_cursor,
                "buffer_start_cursor": self.buffer_start_cursor,
                "cursor_reset": int(cursor or 0) < self.buffer_start_cursor,
                "command": self._command_payload(),
            }

    def kill(self, signal_name: str = "SIGTERM") -> dict[str, object]:
        if self.proc is None or self.pid is None:
            return {"error": f"process is not running: {self.id}"}

        if signal_name not in _ALLOWED_SIGNALS:
            return {
                "error": f"unsupported signal: {signal_name} "
                f"(use one of {', '.join(sorted(_ALLOWED_SIGNALS))})"
            }
        signal_value = getattr(signal, signal_name, None)
        if signal_value is None:
            return {"error": f"unknown signal: {signal_name}"}

        if signal_name == "SIGINT" and self.master_fd is not None and self.is_alive():
            try:
                os.write(self.master_fd, b"\x03")
            except OSError:
                pass
            with self.lock:
                if self.active_command and self.active_command.get("status") == "running":
                    self.active_command["status"] = "completed"
                    self.active_command["exit_code"] = 130
                    self.active_command["completed_at"] = time.time()
                    self.status = "idle"
        else:
            try:
                os.killpg(os.getpgid(self.pid), signal_value)
            except ProcessLookupError:
                pass

        if signal_name == "SIGKILL":
            self._mark_exited(exit_code=self._wait_for_exit(timeout=0.2))

        return {
            "process_id": self.id,
            "status": self._current_status(),
            "shell_alive": self.is_alive(),
            "command": self._command_payload(),
        }

    def close(self) -> None:
        if self.is_alive():
            self.kill("SIGTERM")
            self._wait_for_exit(timeout=0.2)
        if self.is_alive():
            self.kill("SIGKILL")
            self._wait_for_exit(timeout=0.2)
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None

    def _write(self, text: str) -> None:
        if self.master_fd is None:
            raise RuntimeError("PTY is not initialized")
        data = text.encode("utf-8")
        os.write(self.master_fd, data)
        self.last_activity_at = time.time()

    def _reader_loop(self) -> None:
        while True:
            if self.master_fd is None:
                break

            if self.proc is not None and self.proc.poll() is not None:
                self._drain_output()
                self._mark_exited(exit_code=self._wait_for_exit(timeout=0.1))
                break

            try:
                ready, _, _ = select.select([self.master_fd], [], [], 0.1)
            except (OSError, ValueError):
                break

            if not ready:
                continue

            try:
                chunk = os.read(self.master_fd, _READ_CHUNK_SIZE)
            except BlockingIOError:
                continue
            except OSError as exc:
                if exc.errno == errno.EIO:
                    self._mark_exited(exit_code=self._wait_for_exit(timeout=0.1))
                    break
                break

            if not chunk:
                self._mark_exited(exit_code=self._wait_for_exit(timeout=0.1))
                break

            text = chunk.decode("utf-8", errors="replace")
            self._append_output(text)

    def _drain_output(self) -> None:
        if self.master_fd is None:
            return
        while True:
            try:
                chunk = os.read(self.master_fd, _READ_CHUNK_SIZE)
            except BlockingIOError:
                return
            except OSError as exc:
                if exc.errno == errno.EIO:
                    return
                return
            if not chunk:
                return
            self._append_output(chunk.decode("utf-8", errors="replace"))

    def _append_output(self, text: str) -> None:
        with self.lock:
            self.buffer += text
            if len(self.buffer) > self.output_limit:
                trim = len(self.buffer) - self.output_limit
                self.buffer = self.buffer[trim:]
                self.buffer_start_cursor += trim
                self.scan_pos = max(self.scan_pos - trim, 0)

            self.last_activity_at = time.time()
            self._update_command_state_locked()

    def _update_command_state_locked(self) -> None:
        if not self.active_command:
            return
        if self.active_command.get("status") != "running":
            return

        # Only scan buffer appended since the last pass (with a small overlap
        # for markers split across chunks) — a whole-buffer rescan per append
        # is O(n^2) on chatty processes.
        scan_start = max(self.scan_pos - 80, 0)
        self.scan_pos = len(self.buffer)
        for marker in _DONE_MARKER_RE.finditer(self.buffer, scan_start):
            command_id = marker.group(1)
            if command_id != self.active_command.get("id"):
                continue

            self.active_command["status"] = "completed"
            self.active_command["exit_code"] = int(marker.group(2))
            self.active_command["completed_at"] = time.time()
            if self.proc is not None and self.proc.poll() is None:
                self.status = "idle"
            return

    def _mark_exited(self, exit_code: int | None) -> None:
        with self.lock:
            self.status = "exited"
            self.last_activity_at = time.time()
            if self.active_command and self.active_command.get("status") == "running":
                self.active_command["status"] = "completed"
                self.active_command["exit_code"] = exit_code
                self.active_command["completed_at"] = time.time()

    def _wait_for_exit(self, timeout: float = 0.0) -> int | None:
        if self.proc is None:
            return None
        if self.proc.poll() is not None:
            return self.proc.returncode
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return self.proc.returncode

    def _current_status(self) -> str:
        if not self.is_alive():
            return "exited"
        return self.status

    def _command_payload(self) -> dict[str, object] | None:
        if not self.active_command:
            return None
        return {
            "id": self.active_command.get("id"),
            "status": self.active_command.get("status"),
            "exit_code": self.active_command.get("exit_code"),
        }


class ProcessManager:
    def __init__(self) -> None:
        self._sessions: dict[str, ProcessSession] = {}
        self._lock = threading.Lock()

    def spawn(
        self,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        shell: str | None = None,
    ) -> dict[str, object]:
        if cwd is None:
            resolved_cwd = default_chat_files_cwd()
        else:
            try:
                resolved_cwd = resolve_chat_files_path(cwd)
            except PathAccessError as exc:
                return {"error": str(exc)}
        if not resolved_cwd.exists():
            return {"error": f"path does not exist: {resolved_cwd}"}
        if not resolved_cwd.is_dir():
            return {"error": f"path is not a directory: {resolved_cwd}"}
        try:
            ensure_host_execution_allowed(resolved_cwd)
        except AppHostExecutionDenied as exc:
            return {"error": str(exc)}

        # Prune dead sessions, then enforce the concurrency cap so leaked
        # sessions can't accumulate forever in a long-lived server process.
        stale: list[ProcessSession] = []
        with self._lock:
            for session_id, existing in list(self._sessions.items()):
                if not existing.is_alive() and time.time() - existing.last_activity_at > 60:
                    stale.append(self._sessions.pop(session_id))
            active_count = len(self._sessions)
        for existing in stale:
            existing.close()
        if active_count >= _MAX_SESSIONS:
            with self._lock:
                session_ids = ", ".join(self._sessions)
            return {
                "error": f"too many process sessions ({active_count}/{_MAX_SESSIONS}). "
                f"Kill one first with process_kill. Active: {session_ids}"
            }

        try:
            session = ProcessSession(
                cwd=str(resolved_cwd),
                env=_normalize_env(env),
                shell=(shell or _DEFAULT_SHELL).strip() or _DEFAULT_SHELL,
                protected_roots=tuple(str(path) for path in protected_app_roots()),
            ).start()
        except FileNotFoundError:
            return {"error": f"shell not found: {(shell or _DEFAULT_SHELL).strip() or _DEFAULT_SHELL}"}
        except Exception as exc:
            return {"error": f"failed to spawn process: {exc}"}
        with self._lock:
            self._sessions[session.id] = session
        return session.summary()

    def list(self) -> list[dict[str, object]]:
        with self._lock:
            return [session.summary() for session in self._sessions.values()]

    def send(
        self,
        process_id: str | None,
        command: str | None = None,
        input_text: str | None = None,
    ) -> dict[str, object]:
        session = self.get(process_id)
        if isinstance(session, dict):
            return session
        current_roots = tuple(str(path) for path in protected_app_roots())
        if current_roots != session.protected_roots:
            session.close()
            with self._lock:
                self._sessions.pop(session.id, None)
            return {
                "error": (
                    "managed Apps changed after this process session started; "
                    "the stale session was closed to preserve App isolation"
                )
            }
        if command is not None and input_text is not None:
            return {"error": "send accepts either command or input, not both"}
        if command is None and input_text is None:
            return {"error": "send requires command or input"}
        if command is not None:
            return session.send_command(command)
        return session.send_input(input_text)

    def poll(self, process_id: str | None, cursor: int = 0, wait: float = 0) -> dict[str, object]:
        session = self.get(process_id)
        if isinstance(session, dict):
            return session
        # Optional blocking wait: return early when the active command
        # completes (or the shell dies), so callers don't have to spam poll.
        wait = min(max(float(wait or 0), 0.0), _MAX_POLL_WAIT)
        if wait > 0:
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                with session.lock:
                    command = session.active_command
                    running = bool(command and command.get("status") == "running")
                if not running or not session.is_alive():
                    break
                time.sleep(0.15)
        return session.poll(cursor=cursor)

    def kill(self, process_id: str | None, signal_name: str = "SIGTERM") -> dict[str, object]:
        session = self.get(process_id)
        if isinstance(session, dict):
            return session
        return session.kill(signal_name=signal_name)

    def get(self, process_id: str | None) -> ProcessSession | dict[str, str]:
        if not process_id:
            return {"error": "process_id is required"}
        with self._lock:
            session = self._sessions.get(process_id)
        if session is None:
            return {"error": f"unknown process_id: {process_id}"}
        return session

    def close_all(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.close()


_process_manager = ProcessManager()


def close_all_processes():
    """Terminate every PTY session. Called from runtime shutdown so spawned
    shells (own process groups) don't outlive the server as orphans."""
    _process_manager.close_all()


def process_spawn(
    cwd: str = None,
    env: dict[str, str] = None,
    shell: str = None,
):
    return _process_manager.spawn(cwd=cwd, env=env, shell=shell)


def process_list():
    return _process_manager.list()


def process_send(
    process_id: str,
    command: str = None,
    input: str = None,
):
    return _process_manager.send(process_id=process_id, command=command, input_text=input)


def process_poll(
    process_id: str,
    cursor: int = 0,
    wait: float = 0,
):
    """Read incremental output from a PTY session.

    Args:
        process_id: Session id from process_spawn.
        cursor: Resume position from the previous poll's next_cursor.
        wait: Block up to this many seconds (max 30) until the active
            command completes, instead of returning immediately.
    """
    return _process_manager.poll(process_id=process_id, cursor=cursor, wait=wait)


def process_kill(
    process_id: str,
    signal: str = "SIGTERM",
):
    return _process_manager.kill(process_id=process_id, signal_name=signal)


def processes(
    action: str,
    process_id: str = None,
    cwd: str = None,
    env: dict[str, str] = None,
    shell: str = None,
    command: str = None,
    input: str = None,
    cursor: int = 0,
    wait: float = 0,
    signal: str = "SIGTERM",
):
    if action == "spawn":
        return process_spawn(cwd=cwd, env=env, shell=shell)
    if action == "list":
        return process_list()
    if action == "send":
        return process_send(process_id=process_id, command=command, input=input)
    if action == "poll":
        return process_poll(process_id=process_id, cursor=cursor, wait=wait)
    if action == "kill":
        return process_kill(process_id=process_id, signal=signal)
    return {
        "error": "unknown action. Use spawn, list, send, poll, or kill"
    }
