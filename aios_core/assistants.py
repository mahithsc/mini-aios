from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from aios_core.sessions import (
    get_sandbox_dir,
    load_chat_session,
    load_manifest,
    save_chat_session,
    save_manifest,
)
from aios_core.workspace import resolve_workspace_path
from server.types.assistant import Assistant

ASSISTANTS_REGISTRY_PATH = resolve_workspace_path("session/assistants.json")
IDENTITY_FILE_NAME = "IDENTITY.md"
HEARTBEAT_FILE_NAME = "HEARTBEAT.md"
MEMORY_FILE_NAME = "MEMORY.md"
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AssistantContext:
    assistant: Assistant
    identity: str
    memory: str


def _now_ms() -> int:
    return int(datetime.now().timestamp() * 1000)


def _default_title(chat_id: str) -> str:
    return f"Assistant {chat_id[:8]}"


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


def load_assistants_registry() -> list[Assistant]:
    if not ASSISTANTS_REGISTRY_PATH.exists():
        return []

    payload = json.loads(ASSISTANTS_REGISTRY_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        return []

    assistants: list[Assistant] = []
    for record in payload:
        if not isinstance(record, dict):
            continue
        assistant = _assistant_from_record(record)
        if assistant is not None:
            assistants.append(assistant)
    assistants.sort(key=lambda item: item.updatedAt, reverse=True)
    return assistants


def save_assistants_registry(assistants: list[Assistant]) -> None:
    ASSISTANTS_REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    ASSISTANTS_REGISTRY_PATH.write_text(
        json.dumps([assistant.model_dump(mode="json") for assistant in assistants], indent=2),
        encoding="utf-8",
    )


def get_assistant(chat_id: str) -> Assistant | None:
    return next((assistant for assistant in load_assistants_registry() if assistant.chatId == chat_id), None)


def list_assistants() -> list[Assistant]:
    return load_assistants_registry()


def is_assistant_chat(chat_id: str) -> bool:
    return get_assistant(chat_id) is not None


def _assistant_file_paths(chat_id: str) -> tuple[Path, Path, Path]:
    sandbox_dir = get_sandbox_dir(chat_id)
    return (
        sandbox_dir / IDENTITY_FILE_NAME,
        sandbox_dir / HEARTBEAT_FILE_NAME,
        sandbox_dir / MEMORY_FILE_NAME,
    )


def _upsert_manifest_title(chat_id: str, title: str) -> None:
    manifest = load_manifest()
    entry = next((item for item in manifest if item.get("id") == chat_id), None)
    if entry is None:
        return
    entry["title"] = title
    save_manifest(manifest)


def _ensure_assistant_file(path: Path, *, default_content: str) -> str:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(default_content, encoding="utf-8")
        log.warning("Regenerated missing assistant file at %s", path)
    return path.read_text(encoding="utf-8")


def load_assistant_context(chat_id: str) -> AssistantContext | None:
    assistant = get_assistant(chat_id)
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


def initialize_assistant(
    chat_id: str,
    *,
    title: str | None = None,
    identity_body: str | None = None,
    heartbeat_body: str | None = None,
    memory_body: str | None = None,
) -> Assistant:
    existing_messages = load_chat_session(chat_id)
    save_chat_session(chat_id, existing_messages)

    assistant_title = (title or "").strip() or _default_title(chat_id)
    identity_path, heartbeat_path, memory_path = _assistant_file_paths(chat_id)
    sandbox_dir = identity_path.parent
    sandbox_dir.mkdir(parents=True, exist_ok=True)

    if not identity_path.exists() or identity_body is not None:
        identity_path.write_text(
            (identity_body.strip() if isinstance(identity_body, str) and identity_body.strip() else _default_identity(assistant_title)),
            encoding="utf-8",
        )
    if not heartbeat_path.exists() or heartbeat_body is not None:
        heartbeat_path.write_text(
            (heartbeat_body.strip() if isinstance(heartbeat_body, str) and heartbeat_body.strip() else _default_heartbeat()),
            encoding="utf-8",
        )
    if not memory_path.exists() or memory_body is not None:
        memory_path.write_text(
            (memory_body.strip() if isinstance(memory_body, str) and memory_body.strip() else _default_memory(assistant_title)),
            encoding="utf-8",
        )

    now = _now_ms()
    assistants = load_assistants_registry()
    existing = next((assistant for assistant in assistants if assistant.chatId == chat_id), None)

    relative_identity_path = str(identity_path.relative_to(resolve_workspace_path(".")))
    relative_heartbeat_path = str(heartbeat_path.relative_to(resolve_workspace_path(".")))
    relative_memory_path = str(memory_path.relative_to(resolve_workspace_path(".")))

    assistant = Assistant(
        id=chat_id,
        chatId=chat_id,
        title=assistant_title,
        createdAt=existing.createdAt if existing is not None else now,
        updatedAt=now,
        heartbeatEnabled=existing.heartbeatEnabled if existing is not None else False,
        identityPath=relative_identity_path,
        heartbeatPath=relative_heartbeat_path,
        memoryPath=relative_memory_path,
    )

    next_assistants = [item for item in assistants if item.chatId != chat_id]
    next_assistants.append(assistant)
    save_assistants_registry(next_assistants)
    _upsert_manifest_title(chat_id, assistant_title)

    return assistant
