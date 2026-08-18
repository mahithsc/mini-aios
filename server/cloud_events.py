"""Persistent device-side receiver for durable aios-cloud events."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

import websockets

from aios_core.deploy.cloud_client import DEFAULT_CLOUD_URL, paired_device_token
from server.notifications.runtime import get_notification_service

log = logging.getLogger(__name__)
_ENABLED_VALUES = frozenset({"1", "true", "yes", "on"})


def cloud_device_events_enabled() -> bool:
    """Return whether the legacy deployment-event WebSocket is explicitly enabled."""
    return (
        os.getenv("AIOS_CLOUD_DEVICE_EVENTS_ENABLED", "").strip().lower()
        in _ENABLED_VALUES
    )


def _device_websocket_url(base_url: str, token: str) -> str:
    parts = urlsplit(base_url.rstrip("/"))
    scheme = "wss" if parts.scheme == "https" else "ws"
    base_path = parts.path.rstrip("/")
    return urlunsplit(
        (
            scheme,
            parts.netloc,
            f"{base_path}/ws/device",
            urlencode({"token": token}),
            "",
        )
    )


def _notification_copy(
    event_type: str, payload: dict[str, Any]
) -> tuple[str, str, str]:
    if event_type == "deployment.pipeline_completed":
        status = str(payload.get("status", "completed"))
        succeeded = status in {"active", "completed", "succeeded", "success"}
        level = "success" if succeeded else "error"
        return (
            ("App deployment completed" if succeeded else "App deployment failed"),
            f"Deployment pipeline {status}.",
            level,
        )
    component = str(payload.get("component", "app")).replace("_", " ").title()
    status = str(payload.get("status", "updated"))
    error_code = payload.get("error_code")
    if status == "active":
        return (
            f"{component} deployment ready",
            "Deployment is active.",
            "success",
        )
    if status == "failed":
        suffix = f" ({error_code})" if error_code else ""
        return (
            f"{component} deployment failed",
            f"Deployment failed{suffix}.",
            "error",
        )
    if status == "queued":
        return (
            f"{component} deployment started",
            "Prerequisites are complete; deployment is queued.",
            "info",
        )
    return (f"{component} deployment updated", f"Deployment is {status}.", "info")


def persist_cloud_event(event_type: str, payload: dict[str, Any]) -> bool:
    event_id = payload.get("event_id")
    sequence = payload.get("sequence")
    if not isinstance(event_id, str) or not event_id:
        raise ValueError("Cloud event is missing event_id")
    if not isinstance(sequence, int) or sequence < 0:
        raise ValueError("Cloud event has an invalid sequence")
    title, body, level = _notification_copy(event_type, payload)
    return (
        get_notification_service().create_cloud_event_notification(
            event_id=event_id,
            sequence=sequence,
            event_type=event_type,
            payload=payload,
            title=title,
            body=body,
            level=level,
        )
        is not None
    )


def _decode_deployment_message(
    raw: str | bytes,
) -> tuple[str, dict[str, Any]] | None:
    try:
        message = json.loads(raw)
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
        return None
    if not isinstance(message, dict):
        return None
    event_type = message.get("type")
    payload = message.get("payload")
    if (
        not isinstance(event_type, str)
        or not event_type.startswith("deployment.")
        or not isinstance(payload, dict)
    ):
        return None
    return event_type, payload


class CloudDeviceEventService:
    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._stopping: asyncio.Event | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        if not cloud_device_events_enabled():
            log.info(
                "Cloud event receiver disabled; set "
                "AIOS_CLOUD_DEVICE_EVENTS_ENABLED=true to enable it"
            )
            return
        token = (
            os.getenv("AIOS_CLOUD_DEVICE_TOKEN", "").strip() or paired_device_token()
        )
        if not token:
            log.info("Cloud event receiver disabled because this device is not paired")
            return
        base_url = os.getenv("AIOS_CLOUD_URL", "").strip() or DEFAULT_CLOUD_URL
        stopping = asyncio.Event()
        self._stopping = stopping
        self._task = asyncio.create_task(
            self._run(_device_websocket_url(base_url, token), stopping),
            name="aios-cloud-device-events",
        )

    async def shutdown(self) -> None:
        task = self._task
        if task is None:
            return
        if self._stopping is not None:
            self._stopping.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            if self._task is task:
                self._task = None
                self._stopping = None

    async def _run(self, websocket_url: str, stopping: asyncio.Event) -> None:
        retry_seconds = 1.0
        while not stopping.is_set():
            try:
                async with websockets.connect(
                    websocket_url,
                    ping_interval=20,
                    ping_timeout=20,
                ) as socket:
                    retry_seconds = 1.0
                    log.info("Cloud event receiver connected")
                    while not stopping.is_set():
                        try:
                            raw = await asyncio.wait_for(socket.recv(), timeout=30)
                        except TimeoutError:
                            await socket.send(json.dumps({"type": "heartbeat"}))
                            continue
                        decoded = _decode_deployment_message(raw)
                        if decoded is None:
                            continue
                        event_type, payload = decoded
                        try:
                            persist_cloud_event(event_type, payload)
                        except (TypeError, ValueError):
                            log.warning("Rejected malformed cloud deployment event")
                            continue
                        await socket.send(
                            json.dumps(
                                {
                                    "type": "notification.ack",
                                    "payload": {"event_id": payload["event_id"]},
                                }
                            )
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning(
                    "Cloud event receiver disconnected; reconnecting",
                    exc_info=True,
                )
            try:
                await asyncio.wait_for(stopping.wait(), timeout=retry_seconds)
            except TimeoutError:
                pass
            retry_seconds = min(retry_seconds * 2, 30.0)


_cloud_device_events = CloudDeviceEventService()


async def start_cloud_device_events() -> None:
    await _cloud_device_events.start()


async def shutdown_cloud_device_events() -> None:
    await _cloud_device_events.shutdown()
