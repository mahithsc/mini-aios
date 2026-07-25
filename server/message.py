"""HTTP + SSE chat entry point.

`POST /message` is the request/response (no-WebSocket) way to send a user turn
to the box: it persists the turn, submits an agent run, and streams the run's
events back as Server-Sent Events. Mirrors the `/ws` chat path but over plain
HTTP so it works cleanly through the Cloudflare tunnel off-LAN.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from aios_core.sessions import load_chat_session, save_chat_session, update_chat_status
from server.execution.runtime import get_runs_service
from server.types.chat import Chat, ChatMessage, UserMessage
from server.types.run import RunCreateRequest

_POLL_INTERVAL = 0.15
_MAX_DURATION_SECONDS = 600.0
_TERMINAL = {"completed", "error"}


def _get_latest_user_message(chat: Chat) -> UserMessage:
    for message in reversed(chat.messages):
        if isinstance(message, UserMessage):
            return message

    raise ValueError("Chat payload does not contain a user message.")


def _append_user_message(
    messages: list[ChatMessage], user_message: UserMessage
) -> list[ChatMessage]:
    if messages and isinstance(messages[-1], UserMessage) and messages[-1].id == user_message.id:
        return messages

    return [*messages, user_message]


def _conversation_messages_for_turn(chat: Chat) -> list[ChatMessage]:
    """History + latest user turn to send to the model.

    The desktop client sends the full in-memory transcript (including assistant
    tool_call_* events). Older code only re-read disk + appended the latest user
    message, which dropped everything the client had for assistant turns.

    Prefer the client payload when it is at least as long as the persisted
    session so tool results and ordering stay aligned with the UI. If the
    client is shorter (e.g. not yet hydrated), fall back to disk + latest user.
    """
    persisted_messages = load_chat_session(chat.id)
    latest_user_message = _get_latest_user_message(chat)
    client_messages = list(chat.messages)

    if len(client_messages) >= len(persisted_messages):
        return client_messages

    return _append_user_message(persisted_messages, latest_user_message)


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


async def stream_message(chat: Chat, turn_id: str | None) -> AsyncIterator[str]:
    # Persist the turn and submit the run (same as the /ws chat path).
    save_chat_session(chat.id, _conversation_messages_for_turn(chat))
    update_chat_status(chat.id, "streaming")
    run = await get_runs_service().submit_run(
        RunCreateRequest(kind="chat", chatId=chat.id, turnId=turn_id)
    )
    yield _sse({"type": "run.accepted", "data": run.model_dump(mode="json")})

    # Stream the run's events by polling the store by sequence until terminal.
    last_sequence = 0
    elapsed = 0.0
    while elapsed < _MAX_DURATION_SECONDS:
        for event in get_runs_service().resume_events(run.id, last_sequence):
            last_sequence = event.sequence
            yield _sse({"type": "run.event", "data": event.model_dump(mode="json")})
            if event.event.type in _TERMINAL:
                yield _sse({"type": "done"})
                return
        await asyncio.sleep(_POLL_INTERVAL)
        elapsed += _POLL_INTERVAL

    yield _sse({"type": "done"})
