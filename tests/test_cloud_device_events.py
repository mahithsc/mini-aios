from __future__ import annotations

from aios_core.db import get_db_connection, initialize_app_db
from server import cloud_events
from server.notifications.broadcaster import NotificationBroadcaster
from server.notifications.service import NotificationService


def test_cloud_event_is_persisted_once_before_ack(monkeypatch, tmp_path) -> None:
    db_path = str(tmp_path / "aios.db")
    initialize_app_db(db_path)
    service = NotificationService(
        db_path=db_path,
        broadcaster=NotificationBroadcaster(),
    )
    monkeypatch.setattr(
        "server.notifications.runtime._notification_service",
        service,
    )
    payload = {
        "event_id": "evt_one",
        "sequence": 7,
        "app_id": "app_one",
        "pipeline_id": "pip_one",
        "deployment_id": "dep_one",
        "component": "database",
        "status": "active",
    }

    assert cloud_events.persist_cloud_event("deployment.updated", payload) is True
    assert cloud_events.persist_cloud_event("deployment.updated", payload) is False

    with get_db_connection(db_path) as connection:
        event_count = connection.execute(
            "SELECT COUNT(*) FROM cloud_device_events"
        ).fetchone()[0]
        notification = connection.execute(
            "SELECT source, source_id, run_id, level, title FROM notifications"
        ).fetchone()
    assert event_count == 1
    assert notification == (
        "system",
        "evt_one",
        "pip_one",
        "success",
        "Database deployment ready",
    )


def test_cloud_websocket_url_does_not_change_cloud_path() -> None:
    assert cloud_events._device_websocket_url(
        "https://cloud.example/base", "token value"
    ) == "wss://cloud.example/ws/device?token=token+value"
