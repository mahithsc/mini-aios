from __future__ import annotations

from contextvars import ContextVar
from pathlib import Path

from .workspace import default_agent_cwd, resolve_agent_path

_CURRENT_CHAT_ID: ContextVar[str | None] = ContextVar("aios_current_chat_id", default=None)


def push_chat_runtime_context(chat_id: str) -> object:
    """Keep conversation identity as metadata, never as a filesystem root."""
    return _CURRENT_CHAT_ID.set(chat_id)


def pop_chat_runtime_context(token: object) -> None:
    _CURRENT_CHAT_ID.reset(token)


def get_current_chat_id() -> str | None:
    return _CURRENT_CHAT_ID.get()


def resolve_chat_files_path(
    path: str | Path,
    *,
    for_write: bool = False,
) -> Path:
    """Backward-compatible name for the shared agent filesystem resolver."""
    return resolve_agent_path(path, for_write=for_write)


def default_chat_files_cwd() -> Path:
    """Backward-compatible name for the shared applications directory."""
    return default_agent_cwd()
