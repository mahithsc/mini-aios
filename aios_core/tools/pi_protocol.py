"""Small, thread-safe client for Pi's JSONL RPC protocol.

Pi RPC uses one JSON object per *LF*-terminated line.  The protocol is kept in
its own module so framing and request correlation can be tested without
starting the coding agent.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Any, BinaryIO, Callable, Iterator
from uuid import uuid4

DEFAULT_MAX_LINE_BYTES = 4 * 1024 * 1024


class PiProtocolError(RuntimeError):
    """The Pi process emitted malformed RPC data."""


class PiRPCError(RuntimeError):
    """An RPC command was rejected, timed out, or the connection closed."""


def encode_rpc_message(message: dict[str, Any]) -> bytes:
    """Encode one RPC object with exactly one terminating LF."""
    if not isinstance(message, dict):
        raise TypeError("RPC message must be an object")
    return (
        json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def decode_rpc_line(raw_line: bytes, *, max_line_bytes: int = DEFAULT_MAX_LINE_BYTES) -> dict[str, Any]:
    """Decode one complete LF-terminated JSON object.

    Pi explicitly frames on byte ``0x0a`` rather than all Unicode line
    separators.  An optional CR immediately before LF is tolerated.
    """
    if not isinstance(raw_line, bytes):
        raise PiProtocolError("RPC stream must be opened in binary mode")
    if len(raw_line) > max_line_bytes:
        raise PiProtocolError(f"RPC line exceeds {max_line_bytes} byte limit")
    if not raw_line.endswith(b"\n"):
        raise PiProtocolError("RPC stream ended with a non-LF-terminated message")
    payload = raw_line[:-1]
    if payload.endswith(b"\r"):
        payload = payload[:-1]
    if not payload:
        raise PiProtocolError("RPC message is empty")
    try:
        decoded = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PiProtocolError(f"RPC message is not valid UTF-8: {exc}") from exc
    try:
        message = json.loads(decoded)
    except json.JSONDecodeError as exc:
        raise PiProtocolError(f"RPC message is not valid JSON: {exc}") from exc
    if not isinstance(message, dict):
        raise PiProtocolError("RPC message must be a JSON object")
    return message


def iter_rpc_messages(
    stream: BinaryIO, *, max_line_bytes: int = DEFAULT_MAX_LINE_BYTES
) -> Iterator[dict[str, Any]]:
    """Yield strict LF-framed objects until EOF."""
    while True:
        raw_line = stream.readline(max_line_bytes + 1)
        if raw_line == b"":
            return
        yield decode_rpc_line(raw_line, max_line_bytes=max_line_bytes)


@dataclass
class _PendingRequest:
    ready: threading.Event = field(default_factory=threading.Event)
    response: dict[str, Any] | None = None
    error: str | None = None


EventCallback = Callable[[dict[str, Any]], None]
CloseCallback = Callable[[str | None], None]


class PiRPCClient:
    """Correlate Pi RPC responses while forwarding asynchronous events.

    ``request`` is safe to call from multiple threads.  Event callbacks run on
    the reader thread, so callbacks that need to issue another request must do
    that work on a different thread.
    """

    def __init__(
        self,
        stdin: BinaryIO,
        stdout: BinaryIO,
        *,
        on_event: EventCallback,
        on_close: CloseCallback,
        max_line_bytes: int = DEFAULT_MAX_LINE_BYTES,
    ) -> None:
        self._stdin = stdin
        self._stdout = stdout
        self._on_event = on_event
        self._on_close = on_close
        self._max_line_bytes = max_line_bytes
        self._pending: dict[str, _PendingRequest] = {}
        self._pending_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._closed = False
        self._reader: threading.Thread | None = None

    @property
    def closed(self) -> bool:
        with self._pending_lock:
            return self._closed

    def start(self, *, name: str = "pi-rpc-reader") -> None:
        with self._pending_lock:
            if self._reader is not None:
                raise RuntimeError("RPC client has already been started")
            self._reader = threading.Thread(target=self._read_loop, name=name, daemon=True)
            reader = self._reader
        reader.start()

    def request(self, command: str, *, timeout: float, **parameters: Any) -> dict[str, Any]:
        """Send a command and wait for the response carrying the same id."""
        if not isinstance(command, str) or not command:
            raise ValueError("RPC command is required")
        request_id = uuid4().hex
        pending = _PendingRequest()
        with self._pending_lock:
            if self._closed:
                raise PiRPCError("Pi RPC connection is closed")
            self._pending[request_id] = pending

        message = {"id": request_id, "type": command, **parameters}
        try:
            data = encode_rpc_message(message)
            with self._write_lock:
                self._stdin.write(data)
                self._stdin.flush()
        except Exception as exc:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise PiRPCError(f"failed to write Pi RPC command {command}: {exc}") from exc

        if not pending.ready.wait(timeout=max(0.0, float(timeout))):
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise PiRPCError(f"Pi RPC command {command} timed out")
        if pending.error:
            raise PiRPCError(pending.error)
        response = pending.response
        if response is None:  # pragma: no cover - defensive invariant
            raise PiRPCError(f"Pi RPC command {command} completed without a response")
        if response.get("command") != command:
            raise PiRPCError(
                f"Pi RPC response command mismatch: expected {command}, "
                f"got {response.get('command')}"
            )
        if response.get("success") is not True:
            detail = response.get("error") or response.get("message") or "command rejected"
            raise PiRPCError(f"Pi RPC command {command} failed: {detail}")
        return response

    def send(self, message: dict[str, Any]) -> None:
        """Send a protocol message that intentionally has no RPC response.

        Pi uses this for ``extension_ui_response`` messages.  It is separate
        from :meth:`request` because Pi does not acknowledge those responses.
        """
        with self._pending_lock:
            if self._closed:
                raise PiRPCError("Pi RPC connection is closed")
        try:
            data = encode_rpc_message(message)
            with self._write_lock:
                self._stdin.write(data)
                self._stdin.flush()
        except Exception as exc:
            raise PiRPCError(f"failed to write Pi RPC message: {exc}") from exc

    def close(self, reason: str = "Pi RPC connection closed") -> None:
        """Fail all in-flight requests.  Process/pipe closure is owned by the job."""
        self._mark_closed(reason)

    def _read_loop(self) -> None:
        failure: str | None = None
        try:
            for message in iter_rpc_messages(
                self._stdout, max_line_bytes=self._max_line_bytes
            ):
                if message.get("type") == "response" and message.get("id") is not None:
                    request_id = str(message["id"])
                    with self._pending_lock:
                        pending = self._pending.pop(request_id, None)
                    if pending is not None:
                        pending.response = message
                        pending.ready.set()
                        continue
                try:
                    self._on_event(message)
                except Exception:
                    # A presentation/normalization bug must not tear down RPC.
                    continue
        except Exception as exc:
            failure = str(exc)
        finally:
            reason = failure or "Pi RPC stream closed"
            self._mark_closed(reason)
            try:
                self._on_close(failure)
            except Exception:
                pass

    def _mark_closed(self, reason: str) -> None:
        with self._pending_lock:
            if self._closed:
                return
            self._closed = True
            pending = list(self._pending.values())
            self._pending.clear()
        for request in pending:
            request.error = reason
            request.ready.set()


def _excerpt(value: Any, limit: int = 12_000) -> Any:
    """Keep individual poll events bounded while retaining useful detail."""
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        encoded = str(value)
    if len(encoded) <= limit:
        return value
    return encoded[:limit] + f"... [truncated {len(encoded) - limit} chars]"


def normalize_pi_event(event: dict[str, Any]) -> dict[str, Any] | None:
    """Translate Pi's public RPC events into a compact, stable tool shape."""
    event_type = event.get("type")
    if event_type == "agent_start":
        return {"kind": "agent_start"}
    if event_type == "agent_settled":
        return {"kind": "agent_settled"}
    if event_type == "turn_start":
        return {"kind": "turn_start"}
    if event_type == "turn_end":
        return {"kind": "turn_end"}
    if event_type == "tool_execution_start":
        return {
            "kind": "tool_start",
            "tool_call_id": event.get("toolCallId"),
            "tool_name": event.get("toolName"),
            "input": _excerpt(event.get("args")),
        }
    if event_type == "tool_execution_update":
        return {
            "kind": "tool_update",
            "tool_call_id": event.get("toolCallId"),
            "tool_name": event.get("toolName"),
            # Pi documents this as accumulated output, not a delta.
            "output": _excerpt(event.get("partialResult")),
        }
    if event_type == "tool_execution_end":
        return {
            "kind": "tool_end",
            "tool_call_id": event.get("toolCallId"),
            "tool_name": event.get("toolName"),
            "output": _excerpt(event.get("result")),
            "is_error": bool(event.get("isError")),
        }
    if event_type in {
        "compaction_start",
        "compaction_end",
        "auto_retry_start",
        "auto_retry_end",
        "extension_error",
        "queue_update",
    }:
        return {"kind": str(event_type), "detail": _excerpt(event)}
    # Text deltas are accumulated by PiJob instead of being exposed one token
    # at a time.  message_end supplies one bounded, useful progress event.
    if event_type == "message_end":
        message = event.get("message")
        if isinstance(message, dict) and message.get("role") == "assistant":
            return {"kind": "message", "detail": _excerpt(message)}
    return None
