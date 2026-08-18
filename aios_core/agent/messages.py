"""Convert persisted chat messages into OpenAI Responses input items."""

from __future__ import annotations

import base64
import json
import mimetypes
from pathlib import Path

from pydantic import TypeAdapter

from aios_core.attachment_policy import (
    AUDIO_FILE_EXTENSIONS,
    AUDIO_MIME_TYPES,
    TEXT_FILE_EXTENSIONS,
    TEXT_MIME_TYPES,
)
from aios_core.workspace import resolve_workspace_path
from server.types.chat import (
    AssistantMessage,
    ChatMessage,
    MessageAttachment,
    UserMessage,
)

CHAT_MESSAGE_ADAPTER = TypeAdapter(ChatMessage)

type ResponseContentPart = dict[str, str]
type ResponseInputMessage = dict[str, str | list[ResponseContentPart]]

# Cap tool args/results when embedding in plain-text assistant content for the LLM.
_MAX_TOOL_PAYLOAD_CHARS = 8_000
_MAX_ATTACHMENT_TEXT_CHARS = 20_000
_MAX_MESSAGE_CONTENT_CHARS = 40_000
_MAX_HISTORY_CONTENT_CHARS = 120_000
# Leave headroom under the Responses API's combined request limit after JSON
# framing. Excess attachments remain available to the agent by workspace path.
_MAX_INLINE_MEDIA_CHARS = 48 * 1024 * 1024


def _truncate_text(
    text: str, max_chars: int, *, suffix: str = "... (truncated for context)"
) -> str:
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
                f"\n[Tool error: {event.toolName} id={event.toolCallId}]\n"
                f"{event.error}\n"
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


def _attachment_data_uri(path: Path, attachment: MessageAttachment) -> str | None:
    try:
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError:
        return None

    mime_type = (
        attachment.mimeType
        or mimetypes.guess_type(attachment.name)[0]
        or mimetypes.guess_type(path.name)[0]
        or "application/octet-stream"
    )
    return f"data:{mime_type};base64,{encoded}"


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
) -> list[ResponseContentPart]:
    content_parts: list[str] = []
    media_parts: list[ResponseContentPart] = []
    inline_media_chars = 0

    def append_media(part: ResponseContentPart, data_key: str) -> bool:
        nonlocal inline_media_chars
        size = len(part[data_key])
        if inline_media_chars + size > _MAX_INLINE_MEDIA_CHARS:
            return False
        media_parts.append(part)
        inline_media_chars += size
        return True

    def note_omitted(attachment: MessageAttachment) -> None:
        content_parts.append(
            "\n".join(
                [
                    f"[Attachment omitted from inline model input: {attachment.name}]",
                    f"Workspace path: {attachment.filePath}",
                    "The combined inline attachment limit was reached; "
                    "use workspace tools to inspect it.",
                ]
            )
        )

    if message.content.strip():
        content_parts.append(message.content)

    for attachment in message.attachments:
        attachment_path = _resolve_attachment_path(attachment)

        if not attachment_path.exists():
            content_parts.append(f"[Attachment unavailable: {attachment.name}]")
            continue

        if attachment.kind == "image":
            content_parts.append(f"[Attached image: {attachment.name}]")
            data_uri = _attachment_data_uri(attachment_path, attachment)
            if data_uri is not None:
                if not append_media(
                    {
                        "type": "input_image",
                        "detail": "auto",
                        "image_url": data_uri,
                    },
                    "image_url",
                ):
                    note_omitted(attachment)
            continue

        if _is_audio_attachment(attachment):
            content_parts.append(
                _format_attachment_reference(
                    attachment,
                    attachment_path,
                    label="audio",
                    guidance=(
                        "This audio file is available in the workspace. "
                        "If you need a transcript, use tools/code to inspect "
                        "or transcribe it."
                    ),
                )
            )
            continue

        if _is_text_attachment(attachment):
            content_parts.append(_read_attachment_preview(attachment_path, attachment))
            continue

        content_parts.append(f"[Attached file: {attachment.name}]")
        data_uri = _attachment_data_uri(attachment_path, attachment)
        if data_uri is not None:
            if not append_media(
                {
                    "type": "input_file",
                    "file_data": data_uri,
                    "filename": attachment.name,
                },
                "file_data",
            ):
                note_omitted(attachment)

    text = _truncate_text(
        "\n\n".join(part for part in content_parts if part),
        _MAX_MESSAGE_CONTENT_CHARS,
        suffix="... (user message truncated for context)",
    )
    return [{"type": "input_text", "text": text}, *media_parts]


def _to_model_message(message: ChatMessage) -> ResponseInputMessage:
    if isinstance(message, UserMessage):
        return {
            "role": "user",
            "content": _message_content_with_attachments(message),
        }

    if isinstance(message, AssistantMessage):
        return {
            "role": "assistant",
            # Assistant history is model output, not model input. The Responses
            # API accepts the SDK's ``EasyInputMessage`` string form for replayed
            # assistant messages. Using a string also avoids inventing the
            # provider-owned IDs required by a full ``ResponseOutputMessage``.
            "content": _assistant_events_to_openai_content(message),
        }

    raise TypeError(f"Unsupported chat message type: {type(message)!r}")


def _message_text(message: ResponseInputMessage) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    return "".join(
        part.get("text", "")
        for part in content
        if part.get("type") in ("input_text", "output_text")
    )


def _message_media_chars(message: ResponseInputMessage) -> int:
    content = message.get("content", "")
    if isinstance(content, str):
        return 0
    return sum(
        len(part.get("image_url", "")) + len(part.get("file_data", ""))
        for part in content
    )


def _truncate_message_history(
    messages: list[ResponseInputMessage],
) -> list[ResponseInputMessage]:
    kept_messages: list[ResponseInputMessage] = []
    total_chars = 0
    total_media_chars = 0

    for message in reversed(messages):
        content_size = len(_message_text(message))
        media_size = _message_media_chars(message)
        if kept_messages and (
            total_chars + content_size > _MAX_HISTORY_CONTENT_CHARS
            or total_media_chars + media_size > _MAX_INLINE_MEDIA_CHARS
        ):
            break
        kept_messages.append(message)
        total_chars += content_size
        total_media_chars += media_size

    kept_messages.reverse()
    dropped_count = len(messages) - len(kept_messages)
    if dropped_count <= 0:
        return kept_messages

    return [
        {
            "role": "assistant",
            "content": (
                "[Earlier conversation truncated to fit the current context window. "
                f"{dropped_count} older message(s) were omitted. "
                "Focus on the recent messages and ask for missing details if needed.]"
            ),
        },
        *kept_messages,
    ]


def chat_message_to_model_message(message: ChatMessage) -> ResponseInputMessage:
    """Convert one persisted chat message into a Responses API input item."""
    validated = CHAT_MESSAGE_ADAPTER.validate_python(message)
    return _to_model_message(validated)


def legacy_chat_message_to_model_item(
    message: ChatMessage,
) -> ResponseInputMessage | None:
    """Convert a safe legacy row, excluding unusable assistant transcripts.

    The UI database may contain interrupted assistant rows. Those rows are not
    valid conversation history, so only completed assistants with meaningful
    replay content are admitted when seeding canonical model history. User
    messages remain eligible regardless of their old UI status.
    """
    validated = CHAT_MESSAGE_ADAPTER.validate_python(message)
    if isinstance(validated, AssistantMessage):
        if validated.status != "complete":
            return None
        if not _assistant_events_to_openai_content(validated).strip():
            return None
    return _to_model_message(validated)


def format_chat_messages_to_model_messages(
    messages: list[ChatMessage],
) -> list[ResponseInputMessage]:
    model_messages = [chat_message_to_model_message(message) for message in messages]
    return _truncate_message_history(model_messages)
