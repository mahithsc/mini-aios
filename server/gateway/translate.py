from __future__ import annotations

from typing import Any

from server.types.run import RunEvent


class ChatRunEventTranslator:
    """Translate mini-aios RunEvents into gateway (AIOS Agent Gateway) events.

    Stateful: accumulates token text per runId because ChatRunner emits the
    terminal `completed` event with no data — the full assistant text the
    client uses to finalize its draft has to come from the accumulated deltas.
    """

    def __init__(self) -> None:
        self._run_text: dict[str, list[str]] = {}

    def translate(self, event: RunEvent) -> list[tuple[str, dict[str, Any]]]:
        if event.kind != "chat" or not event.chatId or event.assistantId:
            return []

        data = event.event.data or {}
        event_type = event.event.type

        if event_type == "started":
            self._run_text.setdefault(event.runId, [])
            return [("assistant.started", {"raw_type": "started"})]

        if event_type == "token":
            value = data.get("value")
            if not isinstance(value, str):
                return []
            self._run_text.setdefault(event.runId, []).append(value)
            return [("assistant.delta", {"text": value})]

        if event_type == "tool_call_start":
            return [
                (
                    "tool.started",
                    {
                        "name": data.get("toolName"),
                        "tool_id": data.get("toolCallId"),
                        "args": data.get("input"),
                        "raw_type": "tool.start",
                    },
                )
            ]

        if event_type == "tool_call_end":
            return [
                (
                    "tool.completed",
                    {
                        "name": data.get("toolName"),
                        "tool_id": data.get("toolCallId"),
                        "result": data.get("output"),
                        "raw_type": "tool.complete",
                    },
                )
            ]

        if event_type == "subagent_tool_event":
            return [
                (
                    "tool.progress",
                    {
                        "name": data.get("toolName"),
                        "tool_id": data.get("toolCallId"),
                        "context": data.get("childEventType"),
                        "raw_type": "tool.progress",
                    },
                )
            ]

        if event_type == "completed":
            text = "".join(self._run_text.pop(event.runId, []))
            return [("assistant.completed", {"text": text})]

        if event_type == "error":
            self._run_text.pop(event.runId, None)
            error = data.get("error")
            message = error if isinstance(error, str) and error else "Run failed."
            return [("error", {"message": message})]

        if event_type == "cancelled":
            text = "".join(self._run_text.pop(event.runId, []))
            return [
                (
                    "assistant.completed",
                    {
                        "text": text,
                        "raw_type": "cancelled",
                        "reason": data.get("reason"),
                    },
                )
            ]

        return []
