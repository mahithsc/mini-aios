"""Per-run context shared between the agent runtime and function tools."""

from __future__ import annotations

import asyncio
import inspect
import threading
from collections.abc import Awaitable, Callable
from contextlib import suppress
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from ..sessions import (
    ensure_chat_storage_dirs,
    get_chat_scratch_dir,
)
from ..workspace import ensure_workspace_dir, resolve_workspace_path
from .tools.subagent_events import SubagentStreamEvent

EventSink = Callable[[SubagentStreamEvent], Awaitable[None] | None]

_CURRENT_CHAT_ID: ContextVar[str | None] = ContextVar(
    "aios_current_chat_id", default=None
)
_CURRENT_CHAT_SCRATCH_DIR: ContextVar[str | None] = ContextVar(
    "aios_current_chat_scratch_dir", default=None
)
_DATA_ROOT_SENTINELS = {
    "apps",
    "cron_logs",
    "deploy",
    "deployments",
    "memories",
    "projects",
    "runs",
    "session",
    "sessions",
    "skills",
    "state",
    "uploads",
    "workspace",
}
_DATA_SCOPE = "data:"
_SCRATCH_SCOPE = "scratch:"


def push_chat_runtime_context(chat_id: str) -> tuple[object, object]:
    # Chats imported from SQLite may not have ever had filesystem state. Make
    # the per-chat roots real before any prompt or tool receives their paths.
    ensure_chat_storage_dirs(chat_id)
    return (
        _CURRENT_CHAT_ID.set(chat_id),
        _CURRENT_CHAT_SCRATCH_DIR.set(str(get_chat_scratch_dir(chat_id))),
    )


def pop_chat_runtime_context(tokens: tuple[object, object]) -> None:
    chat_token, scratch_token = tokens
    _CURRENT_CHAT_ID.reset(chat_token)
    _CURRENT_CHAT_SCRATCH_DIR.reset(scratch_token)


def get_current_chat_id() -> str | None:
    return _CURRENT_CHAT_ID.get()


def get_current_chat_scratch_dir() -> Path | None:
    value = _CURRENT_CHAT_SCRATCH_DIR.get()
    return Path(value) if value else None


def get_current_chat_files_dir() -> Path | None:
    """Compatibility alias for callers written before scratch was named."""

    return get_current_chat_scratch_dir()


def _is_data_root_relative(path: Path) -> bool:
    return bool(path.parts) and path.parts[0] in _DATA_ROOT_SENTINELS


def _scoped_suffix(raw_value: str, scope: str) -> Path | None:
    normalized = raw_value.replace("\\", "/")
    if normalized == scope:
        return Path(".")
    prefix = f"{scope}/"
    if normalized.startswith(prefix):
        suffix = Path(normalized[len(prefix) :])
        if suffix.is_absolute() or ".." in suffix.parts:
            raise ValueError(f"{scope} paths cannot escape their storage scope")
        return suffix
    if normalized.startswith(scope):
        raise ValueError(
            f"{scope} paths cannot escape their storage scope; use the {prefix}... form"
        )
    return None


def _canonical_data_relative_path(path: Path) -> Path:
    """Translate pre-v1 workspace/session paths to the canonical data layout."""

    parts = path.parts
    if parts[:1] == ("workspace",):
        parts = parts[1:]
    if len(parts) >= 3 and parts[0] in {"session", "sessions"}:
        chat_id, category = parts[1], parts[2]
        suffix = parts[3:]
        if category == "files":
            return Path("sessions", chat_id, "scratch", *suffix)
        if category == "uploads":
            return Path("sessions", chat_id, "uploads", *suffix)
    if len(parts) >= 2 and parts[0] == "uploads":
        return Path("sessions", parts[1], "uploads", *parts[2:])
    return Path(*parts) if parts else Path(".")


def resolve_agent_path(path: str | Path) -> Path:
    """Resolve an agent path against an explicit or implicit storage scope.

    ``scratch:/...`` is always chat-scratch-relative and ``data:/...`` is
    always data-root-relative. Ordinary relative paths default to chat scratch;
    canonical top-level data paths remain accepted for compatibility.
    """

    raw_value = str(path)
    raw_path = Path(raw_value).expanduser()
    if raw_path.is_absolute():
        return raw_path

    data_suffix = _scoped_suffix(raw_value, _DATA_SCOPE)
    if data_suffix is not None:
        return resolve_workspace_path(_canonical_data_relative_path(data_suffix))

    scratch_suffix = _scoped_suffix(raw_value, _SCRATCH_SCOPE)
    if scratch_suffix is not None:
        current_chat_scratch_dir = get_current_chat_scratch_dir()
        if current_chat_scratch_dir is None:
            raise ValueError("scratch: paths require an active chat")
        return current_chat_scratch_dir / scratch_suffix

    current_chat_scratch_dir = get_current_chat_scratch_dir()
    if ".." in raw_path.parts:
        raise ValueError("relative agent paths cannot escape their storage scope")
    if current_chat_scratch_dir is not None and not _is_data_root_relative(raw_path):
        return current_chat_scratch_dir / raw_path

    return resolve_workspace_path(_canonical_data_relative_path(raw_path))


def resolve_chat_files_path(path: str | Path) -> Path:
    """Compatibility alias for the former chat-files path resolver."""

    return resolve_agent_path(path)


def default_agent_cwd() -> Path:
    return get_current_chat_scratch_dir() or ensure_workspace_dir()


def default_chat_files_cwd() -> Path:
    """Compatibility alias for the former chat-files working directory."""

    return default_agent_cwd()


class AgentRuntimeContext:
    """Per-run application context shared with function tools."""

    def __init__(
        self,
        event_sink: EventSink | None = None,
        *,
        conversation_recorder: Any | None = None,
    ) -> None:
        self._event_sink = event_sink
        self.conversation_recorder = conversation_recorder
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_to_current_loop(self) -> None:
        """Bind thread-originated generator events to the active run loop."""
        self._loop = asyncio.get_running_loop()

    async def emit(self, event: SubagentStreamEvent) -> None:
        if self.conversation_recorder is not None:
            await self.conversation_recorder.record_custom_event(event)
        if self._event_sink is None:
            return
        result = self._event_sink(event)
        if inspect.isawaitable(result):
            await result

    def emit_sync(self, event: SubagentStreamEvent) -> None:
        """Forward an event from a synchronous tool's worker thread."""
        if self._event_sink is None and self.conversation_recorder is None:
            return
        loop = self._loop
        if loop is None or not loop.is_running():
            return
        future = asyncio.run_coroutine_threadsafe(self.emit(event), loop)
        future.result()

    def child(self, child_run_id: str) -> AgentRuntimeContext:
        recorder = self.conversation_recorder
        if recorder is not None:
            recorder = recorder.child(child_run_id)
        child = AgentRuntimeContext(
            self._event_sink,
            conversation_recorder=recorder,
        )
        child._loop = self._loop
        return child

    async def record_sdk_event(self, event: object) -> int | None:
        if self.conversation_recorder is None:
            return None
        return await self.conversation_recorder.record_sdk_event(event)

    async def persist_tool_output(
        self,
        call_id: str,
        raw_item: dict[str, Any] | Any,
    ) -> int | None:
        if self.conversation_recorder is None:
            return None
        return await self.conversation_recorder.persist_tool_output(call_id, raw_item)


class FunctionCallContext:
    """Minimal compatibility object for existing tools that accept ``fc``."""

    def __init__(self, call_id: str, runtime_context: AgentRuntimeContext) -> None:
        self.call_id = call_id
        self.runtime_context = runtime_context
        self._cancelled = threading.Event()
        self._cancel_callbacks: list[Callable[[], None]] = []
        self._cancel_lock = threading.Lock()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def add_cancel_callback(self, callback: Callable[[], None]) -> None:
        """Register synchronous cleanup for a thread-backed tool."""
        with self._cancel_lock:
            if not self._cancelled.is_set():
                self._cancel_callbacks.append(callback)
                return
        with suppress(Exception):
            callback()

    def cancel(self) -> None:
        with self._cancel_lock:
            if self._cancelled.is_set():
                return
            self._cancelled.set()
            callbacks = list(self._cancel_callbacks)
            self._cancel_callbacks.clear()
        for callback in callbacks:
            with suppress(Exception):
                callback()


__all__ = [
    "AgentRuntimeContext",
    "FunctionCallContext",
    "default_agent_cwd",
    "default_chat_files_cwd",
    "get_current_chat_files_dir",
    "get_current_chat_id",
    "get_current_chat_scratch_dir",
    "pop_chat_runtime_context",
    "push_chat_runtime_context",
    "resolve_agent_path",
    "resolve_chat_files_path",
]
