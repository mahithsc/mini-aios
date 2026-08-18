from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import HTTPException

from aios_core.db import initialize_app_db
from server.gateway import routes
from server.notifications.broadcaster import NotificationBroadcaster
from server.notifications.service import NotificationService
from server.types.notification import NotificationDismissRequest


def _service(tmp_path) -> NotificationService:
    db_path = str(tmp_path / "aios.db")
    initialize_app_db(db_path)
    return NotificationService(
        db_path=db_path,
        broadcaster=NotificationBroadcaster(),
    )


def test_notification_routes_list_and_dismiss(monkeypatch, tmp_path) -> None:
    service = _service(tmp_path)
    notification = service.create_notification(
        source="system",
        title="Deployment complete",
        body="The app is live.",
        level="success",
    )
    monkeypatch.setattr(routes, "get_notification_service", lambda: service)

    listed = asyncio.run(routes.list_notifications())
    dismissed = asyncio.run(
        routes.dismiss_notification(NotificationDismissRequest(id=notification.id))
    )

    assert listed.notifications == [notification]
    assert dismissed.id == notification.id
    assert dismissed.dismissedAt is not None
    assert asyncio.run(routes.list_notifications()).notifications == []


def test_notification_dismiss_route_returns_not_found(monkeypatch, tmp_path) -> None:
    service = _service(tmp_path)
    monkeypatch.setattr(routes, "get_notification_service", lambda: service)

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            routes.dismiss_notification(NotificationDismissRequest(id="missing"))
        )

    assert error.value.status_code == 404


def test_notification_dismissal_is_idempotent_and_broadcast_once(tmp_path) -> None:
    service = _service(tmp_path)
    notification = service.create_notification(
        source="system",
        title="Deployment complete",
        body="The app is live.",
    )

    async def scenario() -> tuple[int | None, int | None]:
        subscriber = service.broadcaster.subscribe()
        try:
            first = service.dismiss_notification(notification.id)
            assert first is not None
            message = await asyncio.wait_for(subscriber.get(), timeout=1)
            second = service.dismiss_notification(notification.id)
            assert second is not None
            await asyncio.sleep(0)
            assert message["type"] == "notification.dismissed"
            assert subscriber.empty()
            return first.dismissedAt, second.dismissedAt
        finally:
            service.broadcaster.unsubscribe(subscriber)

    first_dismissed_at, second_dismissed_at = asyncio.run(scenario())
    assert first_dismissed_at == second_dismissed_at


def test_notification_sse_streams_live_events(monkeypatch, tmp_path) -> None:
    service = _service(tmp_path)
    monkeypatch.setattr(routes, "get_notification_service", lambda: service)

    async def scenario() -> tuple[str, dict]:
        response = await routes.stream_notification_events()
        stream = response.body_iterator
        snapshot = json.loads((await anext(stream)).split("data: ", 1)[1])
        assert snapshot == {"type": "notification.snapshot", "notifications": []}
        pending = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        notification = service.create_notification(
            source="system",
            title="Deployment started",
            body="The pipeline is running.",
        )
        chunk = await asyncio.wait_for(pending, timeout=1)
        await stream.aclose()
        return notification.id, json.loads(chunk.split("data: ", 1)[1])

    notification_id, event = asyncio.run(scenario())

    assert event["type"] == "notification.created"
    assert event["notification"]["id"] == notification_id


def test_notification_sse_begins_with_active_snapshot(monkeypatch, tmp_path) -> None:
    service = _service(tmp_path)
    existing = service.create_notification(
        source="system",
        title="Existing notification",
        body="Created before the stream connected.",
    )
    monkeypatch.setattr(routes, "get_notification_service", lambda: service)

    async def first_event() -> dict:
        response = await routes.stream_notification_events()
        stream = response.body_iterator
        try:
            return json.loads((await anext(stream)).split("data: ", 1)[1])
        finally:
            await stream.aclose()

    snapshot = asyncio.run(first_event())
    assert snapshot["type"] == "notification.snapshot"
    assert [item["id"] for item in snapshot["notifications"]] == [existing.id]
