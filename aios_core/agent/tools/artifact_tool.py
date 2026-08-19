"""Session-scoped artifact writer exposed to the agent runtime."""

from __future__ import annotations

import mimetypes
import os
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from ...sessions import (
    ensure_chat_artifacts_dir,
    get_chat_artifacts_relative_dir,
)
from ...workspace import get_data_dir
from ..context import get_current_chat_artifacts_dir, get_current_chat_id
from . import file_state
from .filesystem import _atomic_write
from .path_security import validate_within_dir
from .toolcore import current_task_id

DEFAULT_SERVER_BASE_URL = os.getenv(
    "AIOS_SERVER_BASE_URL", "http://localhost:8765"
).rstrip("/")


def _artifact_relative_path(path: str) -> Path | str:
    if not isinstance(path, str) or not path.strip():
        return "path is required"
    if "\x00" in path:
        return "path cannot contain null bytes"

    portable = PurePosixPath(path.strip().replace("\\", "/"))
    if portable.is_absolute():
        return "absolute paths are not allowed"
    if not portable.parts or portable == PurePosixPath("."):
        return "path must name a file"
    if ".." in portable.parts:
        return "path cannot escape the artifacts directory"
    if portable.parts[0].endswith(":"):
        return "absolute paths are not allowed"
    return Path(*portable.parts)


def _artifact_url(chat_relative_root: Path, relative_path: Path) -> str:
    chat_id = chat_relative_root.parts[1]
    encoded_path = quote(relative_path.as_posix(), safe="/")
    return (
        f"{DEFAULT_SERVER_BASE_URL}/session-artifacts/"
        f"{quote(chat_id, safe='')}/{encoded_path}"
    )


def artifact(path: str, content: str) -> dict[str, object]:
    """Write a text artifact beneath the active chat's artifact directory.

    ``path`` may contain nested directories but cannot be absolute or escape
    the active chat. The artifact root and any parent directories are created
    automatically. Existing files are replaced atomically.
    """

    chat_id = get_current_chat_id()
    artifacts_dir = get_current_chat_artifacts_dir()
    if chat_id is None or artifacts_dir is None:
        return {"ok": False, "error": "artifact requires an active chat"}
    if not isinstance(content, str):
        return {"ok": False, "error": "content must be a string"}

    relative_path = _artifact_relative_path(path)
    if isinstance(relative_path, str):
        return {"ok": False, "error": relative_path}

    data_root = get_data_dir()
    session_dir = artifacts_dir.parent
    session_error = validate_within_dir(session_dir, data_root)
    if session_error:
        return {"ok": False, "error": "active chat storage is outside the data root"}
    if session_dir.is_symlink() or artifacts_dir.is_symlink():
        return {"ok": False, "error": "artifact storage cannot be a symlink"}

    try:
        artifacts_dir = ensure_chat_artifacts_dir(chat_id)
    except OSError as exc:
        return {"ok": False, "error": f"cannot create artifact directory: {exc}"}

    root_error = validate_within_dir(artifacts_dir, data_root)
    if root_error:
        return {"ok": False, "error": "artifact storage is outside the data root"}

    target = artifacts_dir / relative_path
    target_error = validate_within_dir(target, artifacts_dir)
    if target_error:
        return {"ok": False, "error": "path cannot escape the artifacts directory"}
    if target.is_dir():
        return {"ok": False, "error": f"path is a directory: {relative_path.as_posix()}"}

    with file_state.lock_path(str(target)):
        try:
            _atomic_write(target, content)
        except OSError as exc:
            return {"ok": False, "error": f"cannot write artifact: {exc}"}
        file_state.note_write(current_task_id(), str(target))

    chat_relative_root = get_chat_artifacts_relative_dir(chat_id)
    file_path = chat_relative_root / relative_path
    mime_type = mimetypes.guess_type(relative_path.name)[0] or "text/plain"
    size_bytes = len(content.encode("utf-8"))
    descriptor = {
        "version": 1,
        "chatId": chat_id,
        "title": relative_path.name,
        "path": relative_path.as_posix(),
        "filePath": file_path.as_posix(),
        "dataPath": f"data:/{file_path.as_posix()}",
        "url": _artifact_url(chat_relative_root, relative_path),
        "mimeType": mime_type,
        "sizeBytes": size_bytes,
    }
    return {
        "ok": True,
        "type": "session_artifact",
        "artifact": descriptor,
        "message": "Artifact written.",
    }


__all__ = ["artifact"]
