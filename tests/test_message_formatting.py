from __future__ import annotations

import base64
from dataclasses import is_dataclass
from pathlib import Path

from aios_core.agent.tools.subagent_events import build_subagent_stream_event
from server.types.chat import AssistantMessage, MessageAttachment, UserMessage
from server.utils import utils


def _attachment(
    path: Path,
    *,
    attachment_id: str,
    kind: str,
    mime_type: str | None,
) -> MessageAttachment:
    return MessageAttachment(
        id=attachment_id,
        kind=kind,
        name=path.name,
        filePath=str(path),
        mimeType=mime_type,
    )


def _text(message: utils.ResponseInputMessage) -> str:
    content = message["content"]
    if isinstance(content, str):
        return content
    assert isinstance(content, list)
    return "".join(
        part.get("text", "")
        for part in content
        if part.get("type") in ("input_text", "output_text")
    )


def _decode_data_uri(data_uri: str) -> bytes:
    _, encoded = data_uri.split(",", 1)
    return base64.b64decode(encoded)


def test_formats_responses_text_image_file_and_audio_inputs(tmp_path: Path) -> None:
    image_path = tmp_path / "photo.png"
    image_bytes = b"\x89PNG\r\n\x1a\nimage"
    image_path.write_bytes(image_bytes)

    text_path = tmp_path / "notes.txt"
    text_path.write_text("remember this", encoding="utf-8")

    audio_path = tmp_path / "voice.mp3"
    audio_path.write_bytes(b"audio")

    file_path = tmp_path / "report.pdf"
    file_bytes = b"%PDF-1.7\nexample"
    file_path.write_bytes(file_bytes)

    missing_path = tmp_path / "missing.png"
    message = UserMessage(
        id="user-1",
        createdAt=1,
        updatedAt=1,
        status="complete",
        content="Please inspect these attachments.",
        attachments=[
            _attachment(
                image_path,
                attachment_id="image",
                kind="image",
                mime_type="image/png",
            ),
            _attachment(
                text_path,
                attachment_id="text",
                kind="file",
                mime_type="text/plain",
            ),
            _attachment(
                audio_path,
                attachment_id="audio",
                kind="audio",
                mime_type="audio/mpeg",
            ),
            _attachment(
                file_path,
                attachment_id="file",
                kind="file",
                mime_type="application/pdf",
            ),
            _attachment(
                missing_path,
                attachment_id="missing",
                kind="image",
                mime_type="image/png",
            ),
        ],
    )

    [formatted] = utils.format_chat_messages_to_model_messages([message])

    assert formatted["role"] == "user"
    content = formatted["content"]
    assert isinstance(content, list)
    assert [part["type"] for part in content] == [
        "input_text",
        "input_image",
        "input_file",
    ]

    text = _text(formatted)
    assert "Please inspect these attachments." in text
    assert "[Attached image: photo.png]" in text
    assert "remember this" in text
    assert "[Attached audio: voice.mp3]" in text
    assert f"Absolute path: {audio_path}" in text
    assert "use tools/code to inspect or transcribe it" in text
    assert "[Attached file: report.pdf]" in text
    assert "[Attachment unavailable: missing.png]" in text

    image_part = content[1]
    assert image_part["detail"] == "auto"
    assert image_part["image_url"].startswith("data:image/png;base64,")
    assert _decode_data_uri(image_part["image_url"]) == image_bytes

    file_part = content[2]
    assert file_part["filename"] == "report.pdf"
    assert file_part["file_data"].startswith("data:application/pdf;base64,")
    assert _decode_data_uri(file_part["file_data"]) == file_bytes


def test_formats_assistant_transcript_as_role_correct_output() -> None:
    message = AssistantMessage(
        id="assistant-1",
        createdAt=1,
        updatedAt=1,
        status="complete",
        events=[
            {"id": "token", "createdAt": 1, "type": "token", "value": "Working"},
            {
                "id": "start",
                "createdAt": 2,
                "type": "tool_call_start",
                "toolCallId": "call-1",
                "toolName": "lookup",
                "input": {"query": "answer"},
            },
            {
                "id": "end",
                "createdAt": 3,
                "type": "tool_call_end",
                "toolCallId": "call-1",
                "toolName": "lookup",
                "output": {"result": 42},
            },
        ],
    )

    [formatted] = utils.format_chat_messages_to_model_messages([message])

    assert formatted["role"] == "assistant"
    assert isinstance(formatted["content"], str)
    text = _text(formatted)
    assert text.startswith("Working")
    assert "[Tool call: lookup id=call-1]" in text
    assert '"query": "answer"' in text
    assert "[Tool result: lookup id=call-1]" in text
    assert '"result": 42' in text


def test_history_truncation_keeps_the_newest_message(monkeypatch) -> None:
    monkeypatch.setattr(utils, "_MAX_HISTORY_CONTENT_CHARS", 6)
    messages = [
        UserMessage(
            id="old",
            createdAt=1,
            updatedAt=1,
            status="complete",
            content="older",
        ),
        UserMessage(
            id="new",
            createdAt=2,
            updatedAt=2,
            status="complete",
            content="recent",
        ),
    ]

    formatted = utils.format_chat_messages_to_model_messages(messages)

    assert len(formatted) == 2
    assert formatted[0]["role"] == "assistant"
    assert isinstance(formatted[0]["content"], str)
    assert "1 older message(s) were omitted" in _text(formatted[0])
    assert _text(formatted[1]) == "recent"


def test_public_single_message_helper_preserves_user_attachments(tmp_path: Path) -> None:
    text_path = tmp_path / "context.txt"
    text_path.write_text("supporting context", encoding="utf-8")
    message = UserMessage(
        id="user-single",
        createdAt=1,
        updatedAt=1,
        status="complete",
        content="Read this.",
        attachments=[
            _attachment(
                text_path,
                attachment_id="context",
                kind="file",
                mime_type="text/plain",
            )
        ],
    )

    formatted = utils.chat_message_to_model_message(message)

    assert formatted["role"] == "user"
    assert "Read this." in _text(formatted)
    assert "supporting context" in _text(formatted)


def test_legacy_helper_excludes_unusable_assistant_rows() -> None:
    incomplete = AssistantMessage(
        id="assistant-streaming",
        createdAt=1,
        updatedAt=1,
        status="streaming",
        events=[
            {"id": "token", "createdAt": 1, "type": "token", "value": "partial"}
        ],
    )
    empty = AssistantMessage(
        id="assistant-empty",
        createdAt=2,
        updatedAt=2,
        status="complete",
        events=[
            {"id": "start", "createdAt": 2, "type": "stream_start"},
            {"id": "end", "createdAt": 3, "type": "stream_end"},
        ],
    )
    complete = AssistantMessage(
        id="assistant-complete",
        createdAt=4,
        updatedAt=4,
        status="complete",
        events=[
            {"id": "token", "createdAt": 4, "type": "token", "value": "done"}
        ],
    )

    assert utils.legacy_chat_message_to_model_item(incomplete) is None
    assert utils.legacy_chat_message_to_model_item(empty) is None
    assert utils.legacy_chat_message_to_model_item(complete) == {
        "role": "assistant",
        "content": "done",
    }


def test_inline_media_over_request_budget_falls_back_to_workspace_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    image_path = tmp_path / "large.png"
    image_path.write_bytes(b"not-really-large")
    monkeypatch.setattr(utils, "_MAX_INLINE_MEDIA_CHARS", 20)
    message = UserMessage(
        id="user-large",
        createdAt=1,
        updatedAt=1,
        status="complete",
        content="Inspect this.",
        attachments=[
            _attachment(
                image_path,
                attachment_id="large",
                kind="image",
                mime_type="image/png",
            )
        ],
    )

    [formatted] = utils.format_chat_messages_to_model_messages([message])

    content = formatted["content"]
    assert isinstance(content, list)
    assert [part["type"] for part in content] == ["input_text"]
    assert "omitted from inline model input" in _text(formatted)
    assert str(image_path) in _text(formatted)


def test_history_budget_counts_replayed_inline_media(tmp_path: Path, monkeypatch) -> None:
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    # Either image fits alone; replaying both exceeds the aggregate budget.
    monkeypatch.setattr(utils, "_MAX_INLINE_MEDIA_CHARS", 50)

    def message(message_id: str, path: Path, created_at: int) -> UserMessage:
        return UserMessage(
            id=message_id,
            createdAt=created_at,
            updatedAt=created_at,
            status="complete",
            content=message_id,
            attachments=[
                _attachment(
                    path,
                    attachment_id=f"{message_id}-image",
                    kind="image",
                    mime_type="image/png",
                )
            ],
        )

    formatted = utils.format_chat_messages_to_model_messages(
        [message("old", first, 1), message("new", second, 2)]
    )

    assert len(formatted) == 2
    assert "1 older message(s) were omitted" in _text(formatted[0])
    assert _text(formatted[1]).startswith("new")
    assert [part["type"] for part in formatted[1]["content"]] == [
        "input_text",
        "input_image",
    ]


def test_subagent_stream_event_is_a_framework_free_dataclass() -> None:
    event = build_subagent_stream_event(
        parent_tool_call_id="parent",
        child_run_id="child",
        child_event_type="tool_call_start",
        tool_call_id="tool",
        tool_name="search",
        input={"query": "minimal"},
    )

    assert is_dataclass(event)
    assert event.event == "CustomEvent"
    assert event.kind == "subagent_tool_event"
    assert event.parent_tool_call_id == "parent"
    assert event.tool_name == "search"
    assert str(event) == ""
