from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from aios_core.workspace import resolve_workspace_path
from server.types.assistant import Assistant, AssistantDetail
from server.types.chat import ChatMessage, UserMessage

ASSISTANTS_DIR = resolve_workspace_path("assistants")
ASSISTANTS_REGISTRY_PATH = ASSISTANTS_DIR / "registry.json"
LEGACY_ASSISTANTS_REGISTRY_PATH = resolve_workspace_path("session/assistants.json")
IDENTITY_FILE_NAME = "IDENTITY.md"
HEARTBEAT_FILE_NAME = "HEARTBEAT.md"
MEMORY_FILE_NAME = "MEMORY.md"
ASSISTANT_SESSION_FILE_NAME = "assistant.json"
UPLOADS_DIR_NAME = "uploads"
FILES_DIR_NAME = "files"
ARTIFACTS_DIR_NAME = "artifacts"
CHAT_MESSAGE_ADAPTER = TypeAdapter(ChatMessage)
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AssistantContext:
    assistant: Assistant
    identity: str
    memory: str


def _now_ms() -> int:
    return int(datetime.now().timestamp() * 1000)


def _default_title(assistant_id: str) -> str:
    return f"Assistant {assistant_id[:8]}"


def _sanitize_path_segment(value: str, fallback: str) -> str:
    sanitized = "".join(
        character if character.isalnum() or character in "._-" else "-" for character in value
    )
    sanitized = sanitized.strip("._-")
    return sanitized or fallback


def _default_identity(title: str) -> str:
    return (
        f"# {title}\n\n"
        "## Role\n"
        "- Define the assistant's role here.\n\n"
        "## Scope\n"
        "- Define the domain this assistant owns.\n\n"
        "## Mandate\n"
        "- Describe what this assistant should optimize for over time.\n\n"
        "## Constraints\n"
        "- List boundaries, rules, and constraints.\n\n"
        "## Operating Stance\n"
        "- Describe how this assistant should approach work.\n"
    )


def _default_heartbeat() -> str:
    return (
        "# Heartbeat\n\n"
        "## Review Loop\n"
        "- Review the current sandbox state.\n"
        "- Check for open tasks, recent changes, and unresolved issues.\n"
        "- Decide whether action, notification, or no-op is appropriate.\n\n"
        "## Escalation\n"
        "- Notify the user when something needs attention.\n"
        "- Avoid destructive or irreversible action unless explicitly authorized.\n"
    )


def _default_memory(title: str) -> str:
    return (
        f"# Memory for {title}\n\n"
        "## Important Facts\n"
        "- Add durable facts and observations here.\n\n"
        "## Decisions\n"
        "- Record decisions and why they were made.\n\n"
        "## Open Questions\n"
        "- Track unresolved questions or assumptions.\n"
    )


def _assistant_from_record(record: dict[str, Any]) -> Assistant | None:
    try:
        return Assistant.model_validate(record)
    except Exception:
        return None


def save_assistants_registry(assistants: list[Assistant]) -> None:
    ASSISTANTS_DIR.mkdir(parents=True, exist_ok=True)
    ASSISTANTS_REGISTRY_PATH.write_text(
        json.dumps([assistant.model_dump(mode="json") for assistant in assistants], indent=2),
        encoding="utf-8",
    )


def get_assistant(assistant_id: str) -> Assistant | None:
    return next((assistant for assistant in load_assistants_registry() if assistant.id == assistant_id), None)


def list_assistants() -> list[Assistant]:
    return load_assistants_registry()


def is_assistant_chat(chat_id: str) -> bool:
    return get_assistant(chat_id) is not None


def _assistant_dir(assistant_id: str) -> Path:
    return ASSISTANTS_DIR / _sanitize_path_segment(assistant_id, "assistant")


def _assistant_file_paths(assistant_id: str) -> tuple[Path, Path, Path]:
    sandbox_dir = _assistant_dir(assistant_id)
    return (
        sandbox_dir / IDENTITY_FILE_NAME,
        sandbox_dir / HEARTBEAT_FILE_NAME,
        sandbox_dir / MEMORY_FILE_NAME,
    )


def _assistant_session_path(assistant_id: str) -> Path:
    return _assistant_dir(assistant_id) / ASSISTANT_SESSION_FILE_NAME


def _assistant_uploads_dir(assistant_id: str) -> Path:
    return _assistant_dir(assistant_id) / UPLOADS_DIR_NAME


def _assistant_files_dir(assistant_id: str) -> Path:
    return _assistant_dir(assistant_id) / FILES_DIR_NAME


def _assistant_artifacts_dir(assistant_id: str) -> Path:
    return _assistant_dir(assistant_id) / ARTIFACTS_DIR_NAME


def get_assistant_session_path(assistant_id: str) -> Path:
    return _assistant_session_path(assistant_id)


def get_assistant_files_dir(assistant_id: str) -> Path:
    _ensure_assistant_workspace_dirs(assistant_id)
    return _assistant_files_dir(assistant_id)


def get_assistant_artifacts_dir(assistant_id: str) -> Path:
    _ensure_assistant_workspace_dirs(assistant_id)
    return _assistant_artifacts_dir(assistant_id)


def _ensure_assistant_workspace_dirs(assistant_id: str) -> Path:
    assistant_dir = _assistant_dir(assistant_id)
    assistant_dir.mkdir(parents=True, exist_ok=True)
    _assistant_uploads_dir(assistant_id).mkdir(parents=True, exist_ok=True)
    _assistant_files_dir(assistant_id).mkdir(parents=True, exist_ok=True)
    _assistant_artifacts_dir(assistant_id).mkdir(parents=True, exist_ok=True)
    return assistant_dir


def _relative_to_workspace(path: Path) -> str:
    return str(path.relative_to(resolve_workspace_path(".")))


def _migrate_assistant_storage(assistant: Assistant) -> Assistant:
    _ensure_assistant_workspace_dirs(assistant.id)
    identity_path, heartbeat_path, memory_path = _assistant_file_paths(assistant.id)
    assistant_session_path = _assistant_session_path(assistant.id)
    current_identity_path = resolve_workspace_path(assistant.identityPath)
    current_heartbeat_path = resolve_workspace_path(assistant.heartbeatPath)
    current_memory_path = resolve_workspace_path(assistant.memoryPath)
    legacy_session_path = resolve_workspace_path(
        Path("session") / _sanitize_path_segment(assistant.id, "chat") / "chat.json"
    )

    for current_path, target_path in (
        (current_identity_path, identity_path),
        (current_heartbeat_path, heartbeat_path),
        (current_memory_path, memory_path),
    ):
        if current_path == target_path:
            continue

        target_path.parent.mkdir(parents=True, exist_ok=True)
        if current_path.exists() and not target_path.exists():
            shutil.move(str(current_path), str(target_path))

    if legacy_session_path.exists() and not assistant_session_path.exists():
        assistant_session_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(legacy_session_path), str(assistant_session_path))

    next_identity_path = identity_path if current_identity_path != identity_path else current_identity_path
    next_heartbeat_path = heartbeat_path if current_heartbeat_path != heartbeat_path else current_heartbeat_path
    next_memory_path = memory_path if current_memory_path != memory_path else current_memory_path

    current_relative_identity_path = _relative_to_workspace(next_identity_path)
    current_relative_heartbeat_path = _relative_to_workspace(next_heartbeat_path)
    current_relative_memory_path = _relative_to_workspace(next_memory_path)

    if (
        assistant.identityPath == current_relative_identity_path
        and assistant.heartbeatPath == current_relative_heartbeat_path
        and assistant.memoryPath == current_relative_memory_path
    ):
        return assistant

    return assistant.model_copy(
        update={
            "identityPath": current_relative_identity_path,
            "heartbeatPath": current_relative_heartbeat_path,
            "memoryPath": current_relative_memory_path,
        }
    )


def _ensure_assistant_file(path: Path, *, default_content: str) -> str:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(default_content, encoding="utf-8")
        log.warning("Regenerated missing assistant file at %s", path)
    return path.read_text(encoding="utf-8")


def load_assistant_context(assistant_id: str) -> AssistantContext | None:
    assistant = get_assistant(assistant_id)
    if assistant is None:
        return None

    identity_path = resolve_workspace_path(assistant.identityPath)
    memory_path = resolve_workspace_path(assistant.memoryPath)

    return AssistantContext(
        assistant=assistant,
        identity=_ensure_assistant_file(
            identity_path,
            default_content=_default_identity(assistant.title),
        ),
        memory=_ensure_assistant_file(
            memory_path,
            default_content=_default_memory(assistant.title),
        ),
    )


def load_assistant_session(assistant_id: str) -> list[ChatMessage]:
    session_path = _assistant_session_path(assistant_id)
    if not session_path.exists():
        return []

    try:
        payload = json.loads(session_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []

    if not isinstance(payload, list):
        return []

    messages: list[ChatMessage] = []
    for item in payload:
        try:
            messages.append(CHAT_MESSAGE_ADAPTER.validate_python(item))
        except Exception:
            continue
    return messages


def save_assistant_session(
    assistant_id: str,
    messages: list[ChatMessage],
    *,
    updated_at: int | None = None,
) -> None:
    _ensure_assistant_workspace_dirs(assistant_id)
    session_path = _assistant_session_path(assistant_id)
    session_path.write_text(
        json.dumps([message.model_dump(mode="json") for message in messages], indent=2),
        encoding="utf-8",
    )
    touch_assistant(assistant_id, updated_at=updated_at)


def get_assistant_detail(assistant_id: str) -> AssistantDetail | None:
    assistant = get_assistant(assistant_id)
    if assistant is None:
        return None

    return AssistantDetail(
        **assistant.model_dump(mode="json"),
        messages=load_assistant_session(assistant_id),
    )


def touch_assistant(assistant_id: str, *, updated_at: int | None = None) -> Assistant | None:
    assistants = load_assistants_registry()
    assistant = next((item for item in assistants if item.id == assistant_id), None)
    if assistant is None:
        return None

    next_updated_at = updated_at if updated_at is not None else _now_ms()
    updated_assistant = assistant.model_copy(update={"updatedAt": next_updated_at})
    next_assistants = [item for item in assistants if item.id != assistant_id]
    next_assistants.append(updated_assistant)
    save_assistants_registry(next_assistants)
    return updated_assistant


def create_assistant(
    assistant_id: str,
    *,
    title: str | None = None,
    prompt: str,
    identity_body: str | None = None,
    heartbeat_body: str | None = None,
    memory_body: str | None = None,
) -> AssistantDetail:
    prompt_body = prompt.strip()
    if not prompt_body:
        raise ValueError("Assistant prompt is required.")

    assistant_record = initialize_assistant(
        assistant_id,
        title=title,
        identity_body=identity_body,
        heartbeat_body=heartbeat_body,
        memory_body=memory_body,
    )

    now = _now_ms()
    initial_message = UserMessage(
        id=f"{assistant_id}:prompt",
        createdAt=now,
        updatedAt=now,
        status="complete",
        role="user",
        content=prompt_body,
    )
    save_assistant_session(assistant_id, [initial_message], updated_at=now)
    assistant_record = get_assistant(assistant_id) or assistant_record

    return AssistantDetail(
        **assistant_record.model_dump(mode="json"),
        messages=[initial_message],
    )


def initialize_assistant(
    assistant_id: str,
    *,
    title: str | None = None,
    identity_body: str | None = None,
    heartbeat_body: str | None = None,
    memory_body: str | None = None,
) -> Assistant:
    assistant_title = (title or "").strip() or _default_title(assistant_id)
    identity_path, heartbeat_path, memory_path = _assistant_file_paths(assistant_id)
    _ensure_assistant_workspace_dirs(assistant_id)

    if not identity_path.exists() or identity_body is not None:
        identity_path.write_text(
            (
                identity_body.strip()
                if isinstance(identity_body, str) and identity_body.strip()
                else _default_identity(assistant_title)
            ),
            encoding="utf-8",
        )
    if not heartbeat_path.exists() or heartbeat_body is not None:
        heartbeat_path.write_text(
            (
                heartbeat_body.strip()
                if isinstance(heartbeat_body, str) and heartbeat_body.strip()
                else _default_heartbeat()
            ),
            encoding="utf-8",
        )
    if not memory_path.exists() or memory_body is not None:
        memory_path.write_text(
            (
                memory_body.strip()
                if isinstance(memory_body, str) and memory_body.strip()
                else _default_memory(assistant_title)
            ),
            encoding="utf-8",
        )

    now = _now_ms()
    assistants = load_assistants_registry()
    existing = next((assistant for assistant in assistants if assistant.id == assistant_id), None)

    relative_identity_path = _relative_to_workspace(identity_path)
    relative_heartbeat_path = _relative_to_workspace(heartbeat_path)
    relative_memory_path = _relative_to_workspace(memory_path)

    assistant = Assistant(
        id=assistant_id,
        title=assistant_title,
        createdAt=existing.createdAt if existing is not None else now,
        updatedAt=now,
        heartbeatEnabled=existing.heartbeatEnabled if existing is not None else False,
        identityPath=relative_identity_path,
        heartbeatPath=relative_heartbeat_path,
        memoryPath=relative_memory_path,
    )

    next_assistants = [item for item in assistants if item.id != assistant_id]
    next_assistants.append(assistant)
    save_assistants_registry(next_assistants)

    return assistant


def load_assistants_registry() -> list[Assistant]:
    registry_path = (
        ASSISTANTS_REGISTRY_PATH
        if ASSISTANTS_REGISTRY_PATH.exists()
        else LEGACY_ASSISTANTS_REGISTRY_PATH
    )
    if not registry_path.exists():
        return []

    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        return []

    assistants: list[Assistant] = []
    registry_changed = registry_path != ASSISTANTS_REGISTRY_PATH
    for record in payload:
        if not isinstance(record, dict):
            continue

        assistant = _assistant_from_record(record)
        if assistant is None:
            continue

        migrated_assistant = _migrate_assistant_storage(assistant)
        if migrated_assistant != assistant:
            registry_changed = True
        assistants.append(migrated_assistant)

    assistants.sort(key=lambda item: item.updatedAt, reverse=True)
    if registry_changed:
        save_assistants_registry(assistants)
    return assistants
