"""Codex as an async background job.

``codex_start`` spawns ``codex exec --json`` in the background and returns a job
id immediately; ``codex_poll`` returns streamed progress and the final result
once the job finishes. This removes the blocking-deadline fragility of the
synchronous ``codex_subagent`` tool — a long Codex session no longer ties up the
agent's turn or gets killed at a fixed timeout mid-build.

The shape mirrors the box's process manager (``aios_core/tools/processes.py``):
a :class:`CodexJob` is like a ``ProcessSession``, a :class:`CodexJobManager` like
a ``ProcessManager``, and the agent drives it with the same ``start -> poll``
idiom it already knows from ``process_spawn`` / ``process_poll``. Codex's JSONL
stream is translated with the same pure :func:`translate_codex_event` used by the
synchronous tool.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import uuid4

from ..runtime_context import resolve_chat_files_path
from .codex_subagent import translate_codex_event

_REPO_ROOT = str(Path(__file__).resolve().parents[2])


def _deploy_mcp_config() -> str:
    """codex `-c` value registering the deploy MCP server for the session, so Codex
    can call `deploy(slug)` after building. Uses this interpreter + PYTHONPATH so the
    server can import aios_core."""
    return (
        'mcp_servers.deploy={command="' + sys.executable + '",'
        'args=["-m","aios_core.deploy.mcp_server"],'
        'env={PYTHONPATH="' + _REPO_ROOT + '"}}'
    )

# A generous safety cap only to reap a stuck/zombie Codex — NOT a task deadline.
SAFETY_CAP_SECONDS = float(os.getenv("AIOS_CODEX_SAFETY_CAP", "1800"))  # 30 min
MAX_ACTIVE_JOBS = int(os.getenv("AIOS_CODEX_MAX_JOBS", "6"))


class CodexJob:
    """One background ``codex exec`` session. Thread-safe; a reader thread fills
    ``events`` (command/file activity) and the final message as Codex streams."""

    def __init__(self, job_id: str, task: str, workdir: str, cmd: list[str]) -> None:
        self.id = job_id
        self.task = task
        self.workdir = workdir
        self.cmd = cmd
        self.status = "running"  # running | done | error
        self.error: str | None = None
        self.result: str | None = None
        self.events: list[dict[str, Any]] = []
        self.started_at = monotonic()
        self.finished_at: float | None = None
        self._final_message: str | None = None
        self._text_chunks: list[str] = []
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._new = threading.Event()

    def start(self) -> None:
        self._proc = subprocess.Popen(
            self.cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=self.workdir,
        )
        threading.Thread(target=self._run, daemon=True).start()

    def _finish(self, status: str, *, error: str | None = None, result: str | None = None) -> None:
        with self._lock:
            self.status = status
            self.error = error
            self.result = result
            self.finished_at = monotonic()
        self._new.set()

    def _run(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        deadline = self.started_at + SAFETY_CAP_SECONDS
        try:
            for line in iter(proc.stdout.readline, ""):
                if monotonic() > deadline:
                    proc.kill()
                    self._finish("error", error=f"codex exceeded safety cap {SAFETY_CAP_SECONDS:g}s")
                    return
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except json.JSONDecodeError:
                    self._text_chunks.append(stripped)
                    continue
                for desc in translate_codex_event(obj):
                    if desc["kind"] == "text":
                        self._final_message = desc["value"]
                        self._text_chunks.append(desc["value"])
                    else:  # tool_start / tool_end -> visible progress
                        with self._lock:
                            self.events.append(desc)
                        self._new.set()
            returncode = proc.wait()
            stderr = (proc.stderr.read() if proc.stderr else "") or ""
            if returncode != 0:
                detail = stderr.strip() or "".join(self._text_chunks).strip()
                self._finish("error", error=detail or f"codex exit {returncode}")
            else:
                result = self._final_message or "".join(self._text_chunks).strip() or "(empty)"
                self._finish("done", result=result)
        except Exception as exc:  # pragma: no cover - defensive
            self._finish("error", error=str(exc))

    def poll(self, cursor: int = 0, wait: float = 0.0) -> dict[str, Any]:
        """Return events since ``cursor`` plus status/result. If ``wait`` > 0,
        block up to that many seconds for new events or completion."""
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
                "events": self.events[cursor:],
                "cursor": len(self.events),
                "result": self.result if self.status == "done" else None,
                "error": self.error,
            }

    def stop(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.kill()
        with self._lock:
            if self.status == "running":
                self.status = "error"
                self.error = "stopped by request"
                self.finished_at = monotonic()
        self._new.set()

    def summary(self) -> dict[str, Any]:
        with self._lock:
            return {
                "job_id": self.id,
                "status": self.status,
                "task": self.task[:80],
                "events": len(self.events),
            }


class CodexJobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, CodexJob] = {}
        self._lock = threading.Lock()

    def _active_count(self) -> int:
        return sum(1 for j in self._jobs.values() if j.status == "running")

    def start(self, task: str, path: str = ".", model: str | None = None, enable_deploy: bool = True) -> dict[str, Any]:
        if not isinstance(task, str) or not task.strip():
            return {"error": "task is required"}
        if not isinstance(path, str) or not path.strip():
            return {"error": "path must be a non-empty string"}
        workdir = resolve_chat_files_path(path.strip())
        if not workdir.exists():
            return {"error": f"path does not exist: {workdir}"}
        if not workdir.is_dir():
            return {"error": f"path is not a directory: {workdir}"}

        with self._lock:
            # Reap finished jobs so the active cap only counts live ones.
            if self._active_count() >= MAX_ACTIVE_JOBS:
                running = [jid for jid, j in self._jobs.items() if j.status == "running"]
                return {"error": f"too many active codex jobs ({MAX_ACTIVE_JOBS}); running: {running}"}

        cmd = ["codex", "exec", "--json", "--skip-git-repo-check", "--sandbox", "danger-full-access"]
        if enable_deploy:
            cmd.extend(["-c", _deploy_mcp_config()])
        if isinstance(model, str) and model.strip():
            cmd.extend(["--model", model.strip()])
        cmd.append(task.strip())

        job_id = uuid4().hex[:12]
        job = CodexJob(job_id, task.strip(), str(workdir), cmd)
        try:
            job.start()
        except FileNotFoundError:
            return {"error": "codex CLI is not installed or not on PATH"}
        except Exception as exc:
            return {"error": f"failed to start codex -- {exc}"}
        with self._lock:
            self._jobs[job_id] = job
        return {"job_id": job_id, "status": "running", "workdir": str(workdir)}

    def poll(self, job_id: str, cursor: int = 0, wait: float = 0.0) -> dict[str, Any]:
        job = self._jobs.get(job_id)
        if job is None:
            return {"error": f"unknown job_id: {job_id}"}
        return job.poll(cursor=cursor, wait=wait)

    def stop(self, job_id: str) -> dict[str, Any]:
        job = self._jobs.get(job_id)
        if job is None:
            return {"error": f"unknown job_id: {job_id}"}
        job.stop()
        return {"job_id": job_id, "status": job.status}

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [j.summary() for j in self._jobs.values()]


_manager = CodexJobManager()


def codex_start(task: str | None = None, path: str = ".", model: str | None = None, fc=None) -> dict[str, Any]:
    """Start a Codex coding job in the background and return immediately.

    Delegates a self-contained coding task (implement/edit/refactor/build) to the
    Codex coding agent, running asynchronously so it never blocks this turn. Codex
    cannot see this chat, so ``task`` must be complete and self-contained (name the
    target files and any context). ``path`` is the working directory. Returns a
    ``job_id`` — call ``codex_poll(job_id)`` to watch progress and get the result.
    """
    return _manager.start(task or "", path=path, model=model, enable_deploy=True)


def codex_poll(job_id: str | None = None, cursor: int = 0, wait: float = 0.0, fc=None) -> dict[str, Any]:
    """Check a Codex job started with ``codex_start``.

    Returns the job ``status`` (running/done/error), any new activity events since
    ``cursor`` (commands run, files changed), the updated ``cursor``, and — once
    done — Codex's final ``result``. Pass ``wait`` (seconds) to block briefly for
    new progress instead of returning immediately.
    """
    return _manager.poll(job_id or "", cursor=cursor, wait=wait)


def codex_stop(job_id: str | None = None, fc=None) -> dict[str, Any]:
    """Stop a running Codex job started with ``codex_start``."""
    return _manager.stop(job_id or "")
