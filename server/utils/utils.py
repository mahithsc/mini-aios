from __future__ import annotations

import json
from pathlib import Path

from agno.media import File, Image
from agno.models.message import Message
from aios_core.workspace import resolve_workspace_path
from pydantic import TypeAdapter

from server.types.chat import AssistantMessage, ChatMessage, MessageAttachment, UserMessage
from server.uploads import AUDIO_FILE_EXTENSIONS, AUDIO_MIME_TYPES, TEXT_FILE_EXTENSIONS, TEXT_MIME_TYPES

CHAT_MESSAGE_ADAPTER = TypeAdapter(ChatMessage)

# Cap tool args/results when embedding in plain-text assistant content for the LLM.
_MAX_TOOL_PAYLOAD_CHARS = 8_000
_MAX_ATTACHMENT_TEXT_CHARS = 20_000
_MAX_MESSAGE_CONTENT_CHARS = 40_000
_MAX_HISTORY_CONTENT_CHARS = 120_000


def _truncate_text(text: str, max_chars: int, *, suffix: str = "... (truncated for context)") -> str:
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}\n{suffix}"


def _serialize_tool_payload(payload: object) -> str:
    if payload is None:
        return ""
    if isinstance(payload, str):
        text = payload
    else:
        try:
            text = json.dumps(payload, indent=2, default=str)
        except TypeError:
            text = str(payload)
    return _truncate_text(text, _MAX_TOOL_PAYLOAD_CHARS)


def _assistant_events_to_openai_content(message: AssistantMessage) -> str:
    """Flatten assistant transcript (tokens + tool lifecycle) into one string.

    Native tool-message APIs could be used later; this keeps a single
    user/assistant channel compatible with the current Agent setup.
    """
    parts: list[str] = []
    for event in message.events:
        etype = event.type
        if etype == "token":
            parts.append(event.value)
        elif etype == "tool_call_start":
            args = _serialize_tool_payload(event.input)
            parts.append(
                f"\n\n[Tool call: {event.toolName} id={event.toolCallId}]\n{args}\n"
            )
        elif etype == "tool_call_end":
            out = _serialize_tool_payload(event.output)
            parts.append(
                f"\n[Tool result: {event.toolName} id={event.toolCallId}]\n{out}\n"
            )
        elif etype == "tool_call_error":
            parts.append(
                f"\n[Tool error: {event.toolName} id={event.toolCallId}]\n{event.error}\n"
            )
        elif etype == "stream_error":
            parts.append(f"\n[Stream error]\n{event.error}\n")
        elif etype in ("stream_start", "stream_end"):
            continue
        else:
            continue
    return _truncate_text(
        "".join(parts),
        _MAX_MESSAGE_CONTENT_CHARS,
        suffix="... (assistant transcript truncated for context)",
    )


def _resolve_attachment_path(attachment: MessageAttachment) -> Path:
    return resolve_workspace_path(attachment.filePath)


def _is_text_attachment(attachment: MessageAttachment) -> bool:
    if attachment.mimeType in TEXT_MIME_TYPES:
        return True
    return Path(attachment.name).suffix.lower() in TEXT_FILE_EXTENSIONS


def _is_audio_attachment(attachment: MessageAttachment) -> bool:
    if attachment.kind == "audio":
        return True
    if attachment.mimeType in AUDIO_MIME_TYPES:
        return True
    return Path(attachment.name).suffix.lower() in AUDIO_FILE_EXTENSIONS


def _read_attachment_preview(path: Path, attachment: MessageAttachment) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        return f"[Attached file: {attachment.name}]\nUnable to read attachment: {exc}"

    text = _truncate_text(text, _MAX_ATTACHMENT_TEXT_CHARS)

    return f"[Attached file: {attachment.name}]\n{text}"


def _infer_media_format(path: Path) -> str | None:
    suffix = path.suffix.lstrip(".").lower()
    return suffix or None


def _format_attachment_reference(
    attachment: MessageAttachment,
    path: Path,
    *,
    label: str,
    guidance: str | None = None,
) -> str:
    parts = [
        f"[Attached {label}: {attachment.name}]",
        f"Workspace path: {attachment.filePath}",
        f"Absolute path: {path}",
    ]
    if guidance:
        parts.append(guidance)
    return "\n".join(parts)


def _message_content_with_attachments(
    message: UserMessage,
) -> tuple[str, list[Image], list[File]]:
    content_parts: list[str] = []
    images: list[Image] = []
    files: list[File] = []

    if message.content.strip():
        content_parts.append(message.content)

    for attachment in message.attachments:
        attachment_path = _resolve_attachment_path(attachment)

        if not attachment_path.exists():
            content_parts.append(f"[Attachment unavailable: {attachment.name}]")
            continue

        if attachment.kind == "image":
            content_parts.append(f"[Attached image: {attachment.name}]")
            images.append(
                Image(
                    filepath=attachment_path,
                    mime_type=attachment.mimeType,
                    format=_infer_media_format(attachment_path),
                )
            )
            continue

        if _is_audio_attachment(attachment):
            content_parts.append(
                _format_attachment_reference(
                    attachment,
                    attachment_path,
                    label="audio",
                    guidance=(
                        "This audio file is available in the workspace. "
                        "If you need a transcript, use tools/code to inspect or transcribe it."
                    ),
                )
            )
            continue

        if _is_text_attachment(attachment):
            content_parts.append(_read_attachment_preview(attachment_path, attachment))
            continue

        content_parts.append(f"[Attached file: {attachment.name}]")
        files.append(
            File(
                filepath=attachment_path,
                mime_type=attachment.mimeType,
                filename=attachment.name,
                name=attachment.name,
                format=_infer_media_format(attachment_path),
            )
        )

    return "\n\n".join(part for part in content_parts if part), images, files


def _to_model_message(message: ChatMessage) -> Message:
    if isinstance(message, UserMessage):
        content, images, files = _message_content_with_attachments(message)
        return Message(
            role="user",
            content=_truncate_text(
                content or "",
                _MAX_MESSAGE_CONTENT_CHARS,
                suffix="... (user message truncated for context)",
            ),
            images=images or None,
            files=files or None,
        )

    if isinstance(message, AssistantMessage):
        return Message(
            role="assistant",
            content=_assistant_events_to_openai_content(message),
        )

    raise TypeError(f"Unsupported chat message type: {type(message)!r}")


def _truncate_message_history(messages: list[Message]) -> list[Message]:
    kept_messages: list[Message] = []
    total_chars = 0

    for message in reversed(messages):
        content = message.content or ""
        content_size = len(content)
        if kept_messages and total_chars + content_size > _MAX_HISTORY_CONTENT_CHARS:
            break
        kept_messages.append(message)
        total_chars += content_size

    kept_messages.reverse()
    dropped_count = len(messages) - len(kept_messages)
    if dropped_count <= 0:
        return kept_messages

    return [
        Message(
            role="assistant",
            content=(
                "[Earlier conversation truncated to fit the current context window. "
                f"{dropped_count} older message(s) were omitted. "
                "Focus on the recent messages and ask for missing details if needed.]"
            ),
        ),
        *kept_messages,
    ]


def format_chat_messages_to_model_messages(messages: list[ChatMessage]) -> list[Message]:
    model_messages = [_to_model_message(CHAT_MESSAGE_ADAPTER.validate_python(message)) for message in messages]
    return _truncate_message_history(model_messages)
