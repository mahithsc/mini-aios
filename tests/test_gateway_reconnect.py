from __future__ import annotations

from pathlib import Path

from aios_core.db import initialize_app_db
from server.gateway.bus import GatewayEventBus
from server.gateway.store import list_gateway_events_after


def test_disconnected_client_replays_every_persisted_event(tmp_path: Path) -> None:
    db_path = str(tmp_path / "aios.db")
    initialize_app_db(db_path)
    first_bus = GatewayEventBus(db_path=db_path)
    subscription = first_bus.subscribe("chat-1")

    first = first_bus.publish("chat-1", "assistant.started", {})
    second = first_bus.publish("chat-1", "assistant.delta", {"text": "hello"})
    first_bus.unsubscribe("chat-1", subscription)

    # Publishing continues with no UI subscriber. A newly opened application
    # gets the missed event from SQLite before attaching to the live stream.
    third = first_bus.publish("chat-1", "assistant.completed", {"text": "hello"})
    reopened_bus = GatewayEventBus(db_path=db_path)
    reopened_bus.subscribe("chat-1")

    replay = list_gateway_events_after(
        "chat-1",
        after=second["id"],
        db_path=db_path,
    )

    assert first["id"] < second["id"] < third["id"]
    assert [(event["id"], event["type"]) for event in replay] == [
        (third["id"], "assistant.completed")
    ]
