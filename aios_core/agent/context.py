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

from ..sessions import get_chat_artifacts_dir, get_chat_files_dir
from ..tools.subagent_events import SubagentStreamEvent
from ..workspace import ensure_workspace_dir, resolve_workspace_path

EventSink = Callable[[SubagentStreamEvent], Awaitable[None] | None]

_CURRENT_CHAT_ID: ContextVar[str | None] = ContextVar(
    "aios_current_chat_id", default=None
)
_CURRENT_CHAT_FILES_DIR: ContextVar[str | None] = ContextVar(
    "aios_current_chat_files_dir", default=None
)
_CURRENT_CHAT_ARTIFACTS_DIR: ContextVar[str | None] = ContextVar(
    "aios_current_chat_artifacts_dir", default=None
)
_WORKSPACE_ROOT_SENTINELS = {
    "apps",
    "cron_logs",
    "runs",
    "session",
    "skills",
}


def push_chat_runtime_context(chat_id: str) -> tuple[object, object, object]:
    return (
        _CURRENT_CHAT_ID.set(chat_id),
        _CURRENT_CHAT_FILES_DIR.set(str(get_chat_files_dir(chat_id))),
        _CURRENT_CHAT_ARTIFACTS_DIR.set(str(get_chat_artifacts_dir(chat_id))),
    )


def pop_chat_runtime_context(tokens: tuple[object, object, object]) -> None:
    chat_token, files_token, artifacts_token = tokens
    _CURRENT_CHAT_ID.reset(chat_token)
    _CURRENT_CHAT_FILES_DIR.reset(files_token)
    _CURRENT_CHAT_ARTIFACTS_DIR.reset(artifacts_token)


def get_current_chat_id() -> str | None:
    return _CURRENT_CHAT_ID.get()


def get_current_chat_files_dir() -> Path | None:
    value = _CURRENT_CHAT_FILES_DIR.get()
    return Path(value) if value else None


def get_current_chat_artifacts_dir() -> Path | None:
    value = _CURRENT_CHAT_ARTIFACTS_DIR.get()
    return Path(value) if value else None


def _is_workspace_root_relative(path: Path) -> bool:
    return bool(path.parts) and path.parts[0] in _WORKSPACE_ROOT_SENTINELS


def resolve_chat_files_path(path: str | Path) -> Path:
    raw_path = Path(path).expanduser()
    if raw_path.is_absolute():
        return raw_path

    current_chat_files_dir = get_current_chat_files_dir()
    if current_chat_files_dir is not None and not _is_workspace_root_relative(raw_path):
        return current_chat_files_dir / raw_path

    return resolve_workspace_path(raw_path)


def default_chat_files_cwd() -> Path:
    return get_current_chat_files_dir() or ensure_workspace_dir()


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
    "default_chat_files_cwd",
    "get_current_chat_artifacts_dir",
    "get_current_chat_files_dir",
    "get_current_chat_id",
    "pop_chat_runtime_context",
    "push_chat_runtime_context",
    "resolve_chat_files_path",
]
