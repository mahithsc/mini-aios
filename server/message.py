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

from aios_core.sessions import save_chat_session, update_chat_status
from server.execution.runtime import get_runs_service
from server.types.chat import Chat
from server.types.run import RunCreateRequest
from server.ws.router import _conversation_messages_for_turn

_POLL_INTERVAL = 0.15
_MAX_DURATION_SECONDS = 600.0
_TERMINAL = {"completed", "error"}


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
