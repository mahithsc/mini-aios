from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator

from agno.agent import RunEvent
from pydantic import TypeAdapter

from aios_core.sessions import list_chat_history, load_chat_session, save_chat_session
from server.types.chat import AssistantMessage, Chat, ChatMessage, LLMEvent, UserMessage
from server.types.ws import WSEnvelope
from aios_core.agent import create_agent
from server.utils.utils import format_chat_messages_to_openai_messages

LLM_EVENT_ADAPTER = TypeAdapter(LLMEvent)


def parse_ws_envelope(payload: object) -> WSEnvelope:
    return WSEnvelope.model_validate(payload)


def _event(chat_id: str, event_type: str, **data: object) -> dict[str, object]:
    return {
        "chatId": chat_id,
        "id": str(uuid.uuid4()),
        "createdAt": int(time.time() * 1000),
        "type": event_type,
        **data,
    }


def _append_assistant_event(events: list[LLMEvent], event: LLMEvent) -> None:
    if event.type == "token" and events and events[-1].type == "token":
        previous_event = events[-1]
        events[-1] = previous_event.model_copy(update={"value": previous_event.value + event.value})
        return

    events.append(event)


def _parse_llm_event(payload: dict[str, object]) -> LLMEvent:
    return LLM_EVENT_ADAPTER.validate_python(payload)


def _build_assistant_message(
    events: list[LLMEvent], status: AssistantMessage["status"]
) -> AssistantMessage:
    created_at = events[0].createdAt if events else int(time.time() * 1000)
    updated_at = events[-1].createdAt if events else created_at

    return AssistantMessage(
        id=str(uuid.uuid4()),
        createdAt=created_at,
        updatedAt=updated_at,
        status=status,
        role="assistant",
        events=events,
    )


def _get_latest_user_message(chat: Chat) -> UserMessage:
    for message in reversed(chat.messages):
        if isinstance(message, UserMessage):
            return message

    raise ValueError("Chat payload does not contain a user message.")


def _append_user_message(messages: list[ChatMessage], user_message: UserMessage) -> list[ChatMessage]:
    if messages and isinstance(messages[-1], UserMessage) and messages[-1].id == user_message.id:
        return messages

    return [*messages, user_message]


def _save_assistant_state(
    chat_id: str,
    next_messages: list[ChatMessage],
    assistant_events: list[LLMEvent],
    status: AssistantMessage["status"],
) -> None:
    save_chat_session(
        chat_id,
        [
            *next_messages,
            _build_assistant_message(assistant_events, status=status),
        ],
    )


def _conversation_messages_for_turn(chat: Chat) -> list[ChatMessage]:
    """History + latest user turn to send to the model.

    The desktop client sends the full in-memory transcript (including assistant
    tool_call_* events). Older code only re-read disk + appended the latest user
    message, which dropped everything the client had for assistant turns.

    Prefer the client payload when it is at least as long as the persisted
    session so tool results and ordering stay aligned with the UI. If the
    client is shorter (e.g. not yet hydrated), fall back to disk + latest user.
    """
    persisted_messages = load_chat_session(chat.id)
    latest_user_message = _get_latest_user_message(chat)
    client_messages = list(chat.messages)

    if len(client_messages) >= len(persisted_messages):
        return client_messages

    return _append_user_message(persisted_messages, latest_user_message)


async def router(envelope: WSEnvelope) -> AsyncIterator[dict[str, object]]:
    if envelope.type == "chat-history":
        if isinstance(envelope.data, str):
            chat_id = envelope.data
            chat_history = next((chat for chat in list_chat_history() if chat.id == chat_id), None)

            if chat_history is None:
                return

            yield WSEnvelope(
                type="chat-history",
                data=Chat(
                    id=chat_history.id,
                    title=chat_history.title,
                    createdAt=chat_history.createdAt,
                    updatedAt=chat_history.updatedAt,
                    status=chat_history.status,
                    messages=load_chat_session(chat_id),
                ).model_dump(mode="json"),
            )
            return

        yield WSEnvelope(
            type="chat-history",
            data=[chat.model_dump(mode="json") for chat in list_chat_history()],
        )
        return

    if envelope.type == "chat":
        chat = envelope.data if isinstance(envelope.data, Chat) else Chat.model_validate(envelope.data)
        chat_id = chat.id
        next_messages = _conversation_messages_for_turn(chat)
        assistant_events: list[LLMEvent] = []

        stream_start_event = _event(chat_id, "stream_start")
        yield WSEnvelope(
            type="chat",
            data=stream_start_event,
        )
        _append_assistant_event(assistant_events, _parse_llm_event(stream_start_event))

        try:
            print("Envelope ------->", envelope)
            agent = create_agent()
            messages = format_chat_messages_to_openai_messages(next_messages)

            async for event in agent.arun(messages, stream=True, stream_events=True):
                if event.event == RunEvent.run_content and event.content is not None:
                    token_event = _event(chat_id, "token", value=event.content)
                    _append_assistant_event(assistant_events, _parse_llm_event(token_event))
                    yield WSEnvelope(
                        type="chat",
                        data = token_event
                    )
                elif event.event == RunEvent.run_error:
                    stream_error_event = _event(
                        chat_id,
                        "stream_error",
                        error=event.content or "Agent run failed.",
                    )
                    _append_assistant_event(assistant_events, _parse_llm_event(stream_error_event))
                    _save_assistant_state(chat_id, next_messages, assistant_events, status="error")
                    yield WSEnvelope(
                        type="chat",
                        data=stream_error_event,
                    )
                    return
                elif event.event == RunEvent.tool_call_started:
                    tool = event.tool
                    tool_call_start_event = _event(
                        chat_id,
                        "tool_call_start",
                        toolCallId=str(getattr(tool, "tool_call_id", None) or id(tool)),
                        toolName=tool.tool_name,
                        input=tool.tool_args,
                    )
                    _append_assistant_event(assistant_events, _parse_llm_event(tool_call_start_event))
                    yield WSEnvelope(
                        type="chat",
                        data = tool_call_start_event
                    )
                elif event.event == RunEvent.tool_call_completed:
                    tool = event.tool
                    tool_call_end_event = _event(
                        chat_id,
                        "tool_call_end",
                        toolCallId=str(getattr(tool, "tool_call_id", None) or id(tool)),
                        toolName=tool.tool_name,
                        output=tool.result,
                    )
                    _append_assistant_event(assistant_events, _parse_llm_event(tool_call_end_event))
                    yield WSEnvelope(
                        type="chat",
                        data = tool_call_end_event
                    )
                    
        except Exception as exc:
            stream_error_event = _event(chat_id, "stream_error", error=str(exc))
            _append_assistant_event(assistant_events, _parse_llm_event(stream_error_event))
            _save_assistant_state(chat_id, next_messages, assistant_events, status="error")
            yield WSEnvelope(
                type="chat",
                data = stream_error_event
            )
            return

        if len(assistant_events) == 1:
            stream_error_event = _event(
                chat_id,
                "stream_error",
                error="Agent run ended without producing any output.",
            )
            _append_assistant_event(assistant_events, _parse_llm_event(stream_error_event))
            _save_assistant_state(chat_id, next_messages, assistant_events, status="error")
            yield WSEnvelope(
                type="chat",
                data=stream_error_event,
            )
            return

        stream_end_event = _event(chat_id, "stream_end")
        _append_assistant_event(assistant_events, _parse_llm_event(stream_end_event))
        _save_assistant_state(chat_id, next_messages, assistant_events, status="complete")
        yield WSEnvelope(
            type="chat",
            data = stream_end_event
        ) 