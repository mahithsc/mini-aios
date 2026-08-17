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


def _device_websocket_url(base_url: str, token: str) -> str:
    parts = urlsplit(base_url.rstrip("/"))
    scheme = "wss" if parts.scheme == "https" else "ws"
    return urlunsplit(
        (scheme, parts.netloc, "/ws/device", urlencode({"token": token}), "")
    )


def _notification_copy(
    event_type: str, payload: dict[str, Any]
) -> tuple[str, str, str]:
    if event_type == "deployment.pipeline_completed":
        status = str(payload.get("status", "completed"))
        level = "success" if status == "active" else "error"
        return (
            (
                "App deployment completed"
                if status == "active"
                else "App deployment failed"
            ),
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
        return (f"{component} deployment failed", f"Deployment failed{suffix}.", "error")
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


class CloudDeviceEventService:
    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        token = (
            os.getenv("AIOS_CLOUD_DEVICE_TOKEN", "").strip()
            or paired_device_token()
        )
        if not token:
            log.info("Cloud event receiver disabled because this device is not paired")
            return
        base_url = os.getenv("AIOS_CLOUD_URL", "").strip() or DEFAULT_CLOUD_URL
        self._stopping.clear()
        self._task = asyncio.create_task(
            self._run(_device_websocket_url(base_url, token)),
            name="aios-cloud-device-events",
        )

    async def shutdown(self) -> None:
        self._stopping.set()
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run(self, websocket_url: str) -> None:
        retry_seconds = 1.0
        while not self._stopping.is_set():
            try:
                async with websockets.connect(
                    websocket_url,
                    ping_interval=20,
                    ping_timeout=20,
                ) as socket:
                    retry_seconds = 1.0
                    log.info("Cloud event receiver connected")
                    while not self._stopping.is_set():
                        try:
                            raw = await asyncio.wait_for(socket.recv(), timeout=30)
                        except TimeoutError:
                            await socket.send(json.dumps({"type": "heartbeat"}))
                            continue
                        message = json.loads(raw)
                        if not isinstance(message, dict):
                            continue
                        event_type = message.get("type")
                        payload = message.get("payload")
                        if not isinstance(event_type, str) or not isinstance(
                            payload, dict
                        ):
                            continue
                        if not event_type.startswith("deployment."):
                            continue
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
                await asyncio.wait_for(self._stopping.wait(), timeout=retry_seconds)
            except TimeoutError:
                pass
            retry_seconds = min(retry_seconds * 2, 30.0)


_cloud_device_events = CloudDeviceEventService()


async def start_cloud_device_events() -> None:
    await _cloud_device_events.start()


async def shutdown_cloud_device_events() -> None:
    await _cloud_device_events.shutdown()
