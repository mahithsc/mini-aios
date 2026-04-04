from __future__ import annotations

from aios_core.assistants import get_assistant, initialize_assistant, list_assistants
from aios_core.runtime_context import get_current_chat_id


def assistant(
    action: str,
    title: str = None,
    identity: str = None,
    heartbeat: str = None,
    memory: str = None,
):
    current_chat_id = get_current_chat_id()
    if not current_chat_id:
        return "error: assistant tool requires an active chat context"

    if action == "init":
        assistant_record = initialize_assistant(
            current_chat_id,
            title=title,
            identity_body=identity,
            heartbeat_body=heartbeat,
            memory_body=memory,
        )
        return {
            "assistant": assistant_record.model_dump(mode="json"),
            "message": f"Initialized assistant '{assistant_record.title}' for chat {current_chat_id}.",
        }

    if action == "get":
        assistant_record = get_assistant(current_chat_id)
        if assistant_record is None:
            return "error: current chat is not registered as an assistant"
        return assistant_record.model_dump(mode="json")

    if action == "list":
        return {"assistants": [item.model_dump(mode="json") for item in list_assistants()]}

    return "error: unknown action. Use init, get, or list."
