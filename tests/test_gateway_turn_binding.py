from __future__ import annotations

import asyncio

from server.gateway import routes
from server.gateway.schemas import MessageCreate


def test_gateway_commits_user_before_queuing_exact_turn(monkeypatch) -> None:
    committed = []
    order: list[object] = []

    monkeypatch.setattr(routes, "_get_chat_or_404", lambda _chat_id: object())

    def append_user(_chat_id, message, *, chat_status):
        assert chat_status == "streaming"
        committed.append(message)
        order.append("ui-committed")

    monkeypatch.setattr(routes, "append_user_message", append_user)

    class Bus:
        def publish(self, session_id, event_type, payload):
            order.append(("publish", session_id, event_type, payload))

    monkeypatch.setattr(routes, "get_gateway_bus", Bus)

    class RunsService:
        async def submit_run(self, request):
            order.append(("submit", request))

    monkeypatch.setattr(routes, "get_runs_service", RunsService)

    result = asyncio.run(
        routes.submit_message("chat-1", MessageCreate(content="current question"))
    )

    assert result.status == "accepted"
    assert order[0] == "ui-committed"
    submit = next(entry[1] for entry in order if entry[0] == "submit")
    assert submit.chatId == "chat-1"
    assert submit.sourceId == committed[0].id
    assert submit.turnId == committed[0].id
    assert order.index(next(entry for entry in order if entry[0] == "publish")) < order.index(
        next(entry for entry in order if entry[0] == "submit")
    )
