from __future__ import annotations

import asyncio
import sqlite3

import pytest

from aios_core.db import get_db_connection, initialize_app_db
from server import cloud_events
from server.notifications.broadcaster import NotificationBroadcaster
from server.notifications.service import NotificationService
from server.types.notification import Notification


def test_cloud_event_is_persisted_once_before_ack(monkeypatch, tmp_path) -> None:
    db_path = str(tmp_path / "aios.db")
    initialize_app_db(db_path)
    broadcaster = NotificationBroadcaster()
    service = NotificationService(
        db_path=db_path,
        broadcaster=broadcaster,
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

    async def persist_and_receive() -> dict[str, object]:
        subscriber = broadcaster.subscribe()
        try:
            assert (
                cloud_events.persist_cloud_event("deployment.updated", payload) is True
            )
            assert (
                cloud_events.persist_cloud_event("deployment.updated", payload) is False
            )
            message = await asyncio.wait_for(subscriber.get(), timeout=1)
            assert subscriber.empty()
            return message
        finally:
            broadcaster.unsubscribe(subscriber)

    broadcast_message = asyncio.run(persist_and_receive())

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
    persisted_notification = service.list_notifications().notifications[0]
    assert broadcast_message == {
        "type": "notification.created",
        "notification": persisted_notification.model_dump(mode="json"),
    }


def test_completed_pipeline_projects_to_success_notification() -> None:
    assert cloud_events._notification_copy(
        "deployment.pipeline_completed", {"status": "completed"}
    ) == (
        "App deployment completed",
        "Deployment pipeline completed.",
        "success",
    )


def test_cloud_websocket_url_preserves_configured_base_path() -> None:
    assert (
        cloud_events._device_websocket_url("https://cloud.example/base", "token value")
        == "wss://cloud.example/base/ws/device?token=token+value"
    )


@pytest.mark.parametrize(
    "raw",
    [
        "not-json",
        "[]",
        '{"type":"command","payload":{}}',
        '{"type":"deployment.updated","payload":[]}',
    ],
)
def test_receiver_ignores_non_deployment_frames_without_reconnecting(raw) -> None:
    assert cloud_events._decode_deployment_message(raw) is None


def test_receiver_decodes_deployment_frame() -> None:
    assert cloud_events._decode_deployment_message(
        '{"type":"deployment.updated","payload":{"event_id":"evt_one"}}'
    ) == ("deployment.updated", {"event_id": "evt_one"})


def test_notification_failure_rolls_back_cloud_event(monkeypatch, tmp_path) -> None:
    db_path = str(tmp_path / "aios.db")
    initialize_app_db(db_path)
    service = NotificationService(
        db_path=db_path,
        broadcaster=NotificationBroadcaster(),
    )
    existing = service.create_notification(
        source="system",
        title="Existing",
        body="Existing notification",
    )
    monkeypatch.setattr(
        "server.notifications.service.uuid.uuid4",
        lambda: existing.id,
    )

    with pytest.raises(sqlite3.IntegrityError):
        service.create_cloud_event_notification(
            event_id="evt_rollback",
            sequence=8,
            event_type="deployment.updated",
            payload={"status": "active"},
            title="Deployment ready",
            body="Deployment is active.",
            level="success",
        )

    with get_db_connection(db_path) as connection:
        event_count = connection.execute(
            "SELECT COUNT(*) FROM cloud_device_events WHERE event_id = ?",
            ("evt_rollback",),
        ).fetchone()[0]
    assert event_count == 0


@pytest.mark.parametrize(
    ("value", "enabled"),
    [
        (None, False),
        ("", False),
        ("false", False),
        ("0", False),
        ("true", True),
        ("1", True),
        ("YES", True),
        ("on", True),
    ],
)
def test_cloud_device_event_receiver_requires_explicit_opt_in(
    monkeypatch, value: str | None, enabled: bool
) -> None:
    if value is None:
        monkeypatch.delenv("AIOS_CLOUD_DEVICE_EVENTS_ENABLED", raising=False)
    else:
        monkeypatch.setenv("AIOS_CLOUD_DEVICE_EVENTS_ENABLED", value)

    assert cloud_events.cloud_device_events_enabled() is enabled


def test_disabled_receiver_does_not_claim_the_device_websocket(monkeypatch) -> None:
    monkeypatch.delenv("AIOS_CLOUD_DEVICE_EVENTS_ENABLED", raising=False)

    def unexpected_pairing_lookup() -> str:
        raise AssertionError("disabled receiver must not load the pairing token")

    monkeypatch.setattr(cloud_events, "paired_device_token", unexpected_pairing_lookup)
    service = cloud_events.CloudDeviceEventService()

    asyncio.run(service.start())

    assert service._task is None


def test_notification_broadcast_marshals_from_worker_thread(tmp_path) -> None:
    db_path = str(tmp_path / "aios.db")
    initialize_app_db(db_path)
    broadcaster = NotificationBroadcaster()
    service = NotificationService(db_path=db_path, broadcaster=broadcaster)

    async def create_and_receive() -> tuple[Notification, dict[str, object]]:
        subscriber = broadcaster.subscribe()
        try:
            notification = await asyncio.to_thread(
                service.create_notification,
                source="system",
                title="Background work finished",
                body="The worker completed.",
            )
            message = await asyncio.wait_for(subscriber.get(), timeout=1)
            return notification, message
        finally:
            broadcaster.unsubscribe(subscriber)

    notification, message = asyncio.run(create_and_receive())
    assert message == {
        "type": "notification.created",
        "notification": notification.model_dump(mode="json"),
    }


def test_notification_broadcaster_bounds_slow_subscriber_queues() -> None:
    broadcaster = NotificationBroadcaster(queue_size=1)
    first = Notification(
        id="notification-one",
        source="system",
        level="info",
        title="First",
        body="First",
        createdAt=1,
        updatedAt=1,
    )
    second = first.model_copy(
        update={"id": "notification-two", "title": "Second", "updatedAt": 2}
    )

    async def publish() -> dict[str, object]:
        subscriber = broadcaster.subscribe()
        try:
            await broadcaster.broadcast_created(first)
            await broadcaster.broadcast_created(second)
            assert subscriber.qsize() == 1
            return subscriber.get_nowait()
        finally:
            broadcaster.unsubscribe(subscriber)

    message = asyncio.run(publish())
    assert message["notification"]["id"] == "notification-two"


def test_receiver_can_restart_on_a_new_event_loop(monkeypatch) -> None:
    monkeypatch.setenv("AIOS_CLOUD_DEVICE_EVENTS_ENABLED", "true")
    monkeypatch.setenv("AIOS_CLOUD_DEVICE_TOKEN", "device-token")
    service = cloud_events.CloudDeviceEventService()
    stopping_events: list[asyncio.Event] = []

    async def wait_until_stopped(websocket_url: str, stopping: asyncio.Event) -> None:
        assert websocket_url.endswith("/ws/device?token=device-token")
        stopping_events.append(stopping)
        await stopping.wait()

    monkeypatch.setattr(service, "_run", wait_until_stopped)

    async def run_once() -> None:
        await service.start()
        await asyncio.sleep(0)
        await service.shutdown()

    asyncio.run(run_once())
    asyncio.run(run_once())

    assert len(stopping_events) == 2
    assert stopping_events[0] is not stopping_events[1]
    assert service._task is None
    assert service._stopping is None
