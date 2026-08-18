from __future__ import annotations

import io
import json
import queue
import threading

import pytest

from aios_core.agent.pi.protocol import (
    PiProtocolError,
    PiRPCClient,
    PiRPCError,
    decode_rpc_line,
    encode_rpc_message,
    iter_rpc_messages,
    normalize_pi_event,
)


class _QueueReader:
    def __init__(self) -> None:
        self.items: queue.Queue[bytes] = queue.Queue()

    def readline(self, _limit: int = -1) -> bytes:
        return self.items.get(timeout=2)

    def push(self, message: dict) -> None:
        self.items.put(encode_rpc_message(message))

    def close(self) -> None:
        self.items.put(b"")


class _CaptureWriter:
    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.data = bytearray()
        self.written = threading.Event()

    def write(self, data: bytes) -> int:
        self.data.extend(data)
        while b"\n" in self.data:
            raw, _, remainder = self.data.partition(b"\n")
            self.data = bytearray(remainder)
            self.messages.append(json.loads(raw))
            self.written.set()
        return len(data)

    def flush(self) -> None:
        return None


def test_encode_and_decode_are_strict_lf_binary_json() -> None:
    encoded = encode_rpc_message({"type": "prompt", "message": "héllo"})
    assert encoded.endswith(b"\n")
    assert not encoded.endswith(b"\n\n")
    assert decode_rpc_line(encoded)["message"] == "héllo"
    assert decode_rpc_line(b'{"ok":true}\r\n') == {"ok": True}


def test_decode_rejects_partial_non_lf_and_non_objects() -> None:
    with pytest.raises(PiProtocolError, match="non-LF"):
        decode_rpc_line(b'{"type":"event"}')
    with pytest.raises(PiProtocolError, match="JSON object"):
        decode_rpc_line(b"[]\n")
    with pytest.raises(PiProtocolError, match="byte limit"):
        decode_rpc_line(b'{"x":"long"}\n', max_line_bytes=4)


def test_unicode_line_separator_does_not_split_frames() -> None:
    raw = '{"text":"before\u2028after"}\n'.encode()
    messages = list(iter_rpc_messages(io.BytesIO(raw)))
    assert messages == [{"text": "before\u2028after"}]


def test_rpc_client_correlates_out_of_order_responses_and_forwards_events() -> None:
    reader = _QueueReader()
    writer = _CaptureWriter()
    events: list[dict] = []
    client = PiRPCClient(writer, reader, on_event=events.append, on_close=lambda _: None)
    client.start()
    results: dict[str, dict] = {}

    def make_request(command: str) -> None:
        results[command] = client.request(command, timeout=2)

    first = threading.Thread(target=make_request, args=("get_state",))
    second = threading.Thread(target=make_request, args=("get_session_stats",))
    first.start()
    second.start()
    while len(writer.messages) < 2:
        writer.written.wait(0.1)
        writer.written.clear()
    requests = {message["type"]: message for message in writer.messages}
    reader.push({"type": "agent_start"})
    reader.push(
        {
            "id": requests["get_session_stats"]["id"],
            "type": "response",
            "command": "get_session_stats",
            "success": True,
            "data": {},
        }
    )
    reader.push(
        {
            "id": requests["get_state"]["id"],
            "type": "response",
            "command": "get_state",
            "success": True,
            "data": {},
        }
    )
    first.join(2)
    second.join(2)
    reader.close()
    assert set(results) == {"get_state", "get_session_stats"}
    assert events == [{"type": "agent_start"}]


def test_rpc_client_reports_rejection_and_command_mismatch() -> None:
    reader = _QueueReader()
    writer = _CaptureWriter()
    client = PiRPCClient(writer, reader, on_event=lambda _: None, on_close=lambda _: None)
    client.start()

    def respond(response: dict) -> None:
        assert writer.written.wait(1)
        request = writer.messages[-1]
        reader.push({"id": request["id"], "type": "response", **response})

    rejection = threading.Thread(
        target=respond,
        args=({"command": "prompt", "success": False, "error": "busy"},),
    )
    rejection.start()
    with pytest.raises(PiRPCError, match="busy"):
        client.request("prompt", timeout=1, message="x")
    rejection.join(1)
    writer.written.clear()

    mismatch = threading.Thread(
        target=respond,
        args=({"command": "abort", "success": True},),
    )
    mismatch.start()
    with pytest.raises(PiRPCError, match="mismatch"):
        client.request("steer", timeout=1, message="x")
    mismatch.join(1)
    reader.close()


def test_fire_and_forget_send_has_no_pending_response() -> None:
    reader = _QueueReader()
    writer = _CaptureWriter()
    client = PiRPCClient(writer, reader, on_event=lambda _: None, on_close=lambda _: None)
    client.start()
    client.send({"type": "extension_ui_response", "id": "ui-1", "cancelled": True})
    assert writer.messages == [
        {"type": "extension_ui_response", "id": "ui-1", "cancelled": True}
    ]
    reader.close()


def test_normalize_uses_current_compaction_event_names() -> None:
    start = normalize_pi_event({"type": "compaction_start", "reason": "threshold"})
    end = normalize_pi_event({"type": "compaction_end", "aborted": False})
    assert start and start["kind"] == "compaction_start"
    assert end and end["kind"] == "compaction_end"
    assert normalize_pi_event({"type": "auto_compaction_start"}) is None
