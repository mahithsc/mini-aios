from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator

from agno.agent import RunEvent
from pydantic import TypeAdapter

from aios_core.sessions import load_chat_session, save_chat_session
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


async def router(envelope: WSEnvelope) -> AsyncIterator[dict[str, object]]:
    if envelope.type == "chat":
        chat = envelope.data if isinstance(envelope.data, Chat) else Chat.model_validate(envelope.data)
        agent = create_agent()
        chat_id = chat.id
        persisted_messages = load_chat_session(chat_id)
        latest_user_message = _get_latest_user_message(chat)
        next_messages = _append_user_message(persisted_messages, latest_user_message)
        assistant_events: list[LLMEvent] = []
        stream_start_event = _event(chat_id, "stream_start")

        print("Envelope ------->", envelope)

        messages = format_chat_messages_to_openai_messages(next_messages)




        events = agent.arun(messages, stream=True, stream_events=True)

        yield WSEnvelope(
            type="chat",
            data = stream_start_event
        )
        _append_assistant_event(assistant_events, _parse_llm_event(stream_start_event))

        try:
            async for event in events:
                if event.event == RunEvent.run_content and event.content is not None:
                    token_event = _event(chat_id, "token", value=event.content)
                    _append_assistant_event(assistant_events, _parse_llm_event(token_event))
                    yield WSEnvelope(
                        type="chat",
                        data = token_event
                    )
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
            save_chat_session(
                chat_id,
                [
                    *next_messages,
                    _build_assistant_message(assistant_events, status="error"),
                ],
            )
            yield WSEnvelope(
                type="chat",
                data = stream_error_event
            )
            return

        stream_end_event = _event(chat_id, "stream_end")
        _append_assistant_event(assistant_events, _parse_llm_event(stream_end_event))
        save_chat_session(
            chat_id,
            [
                *next_messages,
                _build_assistant_message(assistant_events, status="complete"),
            ],
        )
        yield WSEnvelope(
            type="chat",
            data = stream_end_event
        ) 