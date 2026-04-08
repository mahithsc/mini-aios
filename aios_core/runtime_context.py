from __future__ import annotations

from contextvars import ContextVar
from pathlib import Path

from .assistants import get_assistant_artifacts_dir, get_assistant_files_dir
from .sessions import get_chat_artifacts_dir, get_chat_files_dir
from .workspace import ensure_workspace_dir, resolve_workspace_path

_CURRENT_CHAT_ID: ContextVar[str | None] = ContextVar("aios_current_chat_id", default=None)
_CURRENT_CHAT_FILES_DIR: ContextVar[str | None] = ContextVar(
    "aios_current_chat_files_dir", default=None
)
_CURRENT_CHAT_ARTIFACTS_DIR: ContextVar[str | None] = ContextVar(
    "aios_current_chat_artifacts_dir", default=None
)
_WORKSPACE_ROOT_SENTINELS = {
    "assistants",
    "cron_logs",
    "heartbeat_logs",
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


def push_assistant_runtime_context(assistant_id: str) -> tuple[object, object, object]:
    return (
        _CURRENT_CHAT_ID.set(assistant_id),
        _CURRENT_CHAT_FILES_DIR.set(str(get_assistant_files_dir(assistant_id))),
        _CURRENT_CHAT_ARTIFACTS_DIR.set(str(get_assistant_artifacts_dir(assistant_id))),
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
