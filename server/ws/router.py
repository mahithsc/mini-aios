from __future__ import annotations

from collections.abc import AsyncIterator

from aios_core.assistants import (
    create_assistant,
    get_assistant_detail,
    list_assistants,
    load_assistant_session,
    save_assistant_session,
)
from aios_core.crons import cron_manager
from server.auth import AuthenticatedUser
from server.chats import get_chat, list_chats, save_chat
from server.notifications.runtime import get_notification_service
from server.execution.runtime import get_runs_service
from server.types.assistant import AssistantCreateRequest, AssistantSubmitRequest
from server.types.cron import CronUpcomingListResponse
from server.types.chat import Chat, ChatMessage, UserMessage
from server.types.notification import NotificationDismissRequest
from server.types.run import ProcessSnapshotListRequest, RunCreateRequest, RunResumeRequest, RunStopRequest
from server.types.ws import WSEnvelope


def parse_ws_envelope(payload: object) -> WSEnvelope:
    return WSEnvelope.model_validate(payload)


def _get_latest_user_message(messages: list[ChatMessage]) -> UserMessage:
    for message in reversed(messages):
        if isinstance(message, UserMessage):
            return message

    raise ValueError("Chat payload does not contain a user message.")


def _append_user_message(messages: list[ChatMessage], user_message: UserMessage) -> list[ChatMessage]:
    if messages and isinstance(messages[-1], UserMessage) and messages[-1].id == user_message.id:
        return messages

    return [*messages, user_message]


def _conversation_messages_for_turn(user_id: str, chat: Chat) -> list[ChatMessage]:
    """History + latest user turn to send to the model.

    The desktop client sends the full in-memory transcript (including assistant
    tool_call_* events). Prefer that payload when it is at least as long as the
    persisted Supabase transcript so tool results and ordering stay aligned with
    the UI.

    If the client is shorter (e.g. not yet hydrated), fall back to persisted
    history + the latest user turn.
    """
    persisted_chat = get_chat(user_id, chat.id)
    persisted_messages = persisted_chat.messages if persisted_chat is not None else []
    latest_user_message = _get_latest_user_message(chat.messages)
    client_messages = list(chat.messages)

    if len(client_messages) >= len(persisted_messages):
        return client_messages

    return _append_user_message(persisted_messages, latest_user_message)


def _assistant_messages_for_turn(request: AssistantSubmitRequest) -> list[ChatMessage]:
    persisted_messages = load_assistant_session(request.assistantId)
    latest_user_message = _get_latest_user_message(request.messages)
    client_messages = list(request.messages)

    if len(client_messages) >= len(persisted_messages):
        return client_messages

    return _append_user_message(persisted_messages, latest_user_message)


async def router(envelope: WSEnvelope, user: AuthenticatedUser) -> AsyncIterator[dict[str, object]]:
    if envelope.type == "assistant.create":
        if envelope.data is None:
            return

        request = (
            envelope.data
            if isinstance(envelope.data, AssistantCreateRequest)
            else AssistantCreateRequest.model_validate(envelope.data)
        )
        assistant = create_assistant(
            request.id,
            title=request.title,
            prompt=request.prompt,
        )
        yield WSEnvelope(
            type="assistant.create",
            data=assistant.model_dump(mode="json"),
        )
        return

    if envelope.type == "assistant.get":
        if not isinstance(envelope.data, str):
            return

        assistant = get_assistant_detail(envelope.data)
        if assistant is None:
            return

        yield WSEnvelope(
            type="assistant.get",
            data=assistant.model_dump(mode="json"),
        )
        return

    if envelope.type == "assistant.submit":
        if envelope.data is None:
            return

        request = (
            envelope.data
            if isinstance(envelope.data, AssistantSubmitRequest)
            else AssistantSubmitRequest.model_validate(envelope.data)
        )
        if get_assistant_detail(request.assistantId) is None:
            return
        next_messages = _assistant_messages_for_turn(request)
        save_assistant_session(request.assistantId, next_messages)
        await get_runs_service().submit_run(
            RunCreateRequest(
                kind="chat",
                assistantId=request.assistantId,
                turnId=request.turnId,
            ),
            user_id=user.id,
        )
        return

    if envelope.type == "assistant.list":
        yield WSEnvelope(
            type="assistant.list",
            data=[assistant.model_dump(mode="json") for assistant in list_assistants()],
        )
        return

    if envelope.type == "chat-history":
        if isinstance(envelope.data, str):
            chat_id = envelope.data
            chat = get_chat(user.id, chat_id)

            if chat is None:
                return

            yield WSEnvelope(
                type="chat-history",
                data=chat.model_dump(mode="json"),
            )
            return

        yield WSEnvelope(
            type="chat-history",
            data=[chat.model_dump(mode="json") for chat in list_chats(user.id)],
        )
        return

    if envelope.type == "notification.list":
        notifications = get_notification_service().list_notifications()
        yield WSEnvelope(
            type="notification.list",
            data=notifications.model_dump(mode="json"),
        )
        return

    if envelope.type == "cron.upcoming.list":
        yield WSEnvelope(
            type="cron.upcoming.list",
            data=CronUpcomingListResponse(crons=cron_manager.get_upcoming_crons()).model_dump(mode="json"),
        )
        return

    if envelope.type == "process.snapshot.list":
        request = (
            ProcessSnapshotListRequest()
            if envelope.data is None
            else (
                envelope.data
                if isinstance(envelope.data, ProcessSnapshotListRequest)
                else ProcessSnapshotListRequest.model_validate(envelope.data)
            )
        )
        snapshots = get_runs_service().list_recent_runs(
            user_id=user.id,
            statuses=request.statuses,
            kinds=request.kinds,
            limit=request.limit,
        )
        yield WSEnvelope(
            type="process.snapshot.list",
            data=[snapshot.model_dump(mode="json") for snapshot in snapshots],
        )
        return

    if envelope.type == "run.resume":
        if envelope.data is None:
            return

        request = (
            envelope.data
            if isinstance(envelope.data, RunResumeRequest)
            else RunResumeRequest.model_validate(envelope.data)
        )
        yield WSEnvelope(
            type="run.resume",
            data=[
                event.model_dump(mode="json")
                for event in get_runs_service().resume_events(
                    request.runId,
                    request.afterSequence,
                    user_id=user.id,
                )
            ],
        )
        return

    if envelope.type == "notification.dismiss":
        if envelope.data is None:
            return

        dismiss_request = (
            envelope.data
            if isinstance(envelope.data, NotificationDismissRequest)
            else NotificationDismissRequest.model_validate(envelope.data)
        )
        get_notification_service().dismiss_notification(dismiss_request.id)
        return

    if envelope.type == "run.stop":
        if envelope.data is None:
            return

        stop_request = (
            envelope.data
            if isinstance(envelope.data, RunStopRequest)
            else RunStopRequest.model_validate(envelope.data)
        )
        await get_runs_service().stop_run(stop_request.runId, user_id=user.id)
        return

    if envelope.type in {"chat", "chat.submit"}:
        turn_id: str | None = None
        if envelope.type == "chat.submit" and isinstance(envelope.data, dict) and "chat" in envelope.data:
            chat = (
                envelope.data["chat"]
                if isinstance(envelope.data["chat"], Chat)
                else Chat.model_validate(envelope.data["chat"])
            )
            raw_turn_id = envelope.data.get("turnId")
            turn_id = raw_turn_id if isinstance(raw_turn_id, str) else None
        else:
            chat = envelope.data if isinstance(envelope.data, Chat) else Chat.model_validate(envelope.data)
        next_messages = _conversation_messages_for_turn(user.id, chat)
        persisted_chat = get_chat(user.id, chat.id)
        base_chat = persisted_chat if persisted_chat is not None else chat
        save_chat(
            user.id,
            base_chat.model_copy(
                update={
                    "title": base_chat.title or chat.title,
                    "status": "streaming",
                    "messages": next_messages,
                }
            ),
        )
        await get_runs_service().submit_run(
            RunCreateRequest(
                kind="chat",
                chatId=chat.id,
                turnId=turn_id,
            ),
            user_id=user.id,
        )
        return
