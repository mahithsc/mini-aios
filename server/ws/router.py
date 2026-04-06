from __future__ import annotations

from collections.abc import AsyncIterator

from aios_core.assistants import initialize_assistant, list_assistants
from aios_core.crons import cron_manager
from aios_core.sessions import list_chat_history, load_chat_session, save_chat_session, update_chat_status
from server.notifications.runtime import get_notification_service
from server.execution.runtime import get_runs_service
from server.types.assistant import AssistantInitRequest
from server.types.cron import CronUpcomingListResponse
from server.types.chat import Chat, ChatMessage, UserMessage
from server.types.notification import NotificationDismissRequest
from server.types.run import ProcessSnapshotListRequest, RunCreateRequest, RunResumeRequest, RunStopRequest
from server.types.ws import WSEnvelope


def parse_ws_envelope(payload: object) -> WSEnvelope:
    return WSEnvelope.model_validate(payload)


def _get_latest_user_message(chat: Chat) -> UserMessage:
    for message in reversed(chat.messages):
        if isinstance(message, UserMessage):
            return message

    raise ValueError("Chat payload does not contain a user message.")


def _append_user_message(messages: list[ChatMessage], user_message: UserMessage) -> list[ChatMessage]:
    if messages and isinstance(messages[-1], UserMessage) and messages[-1].id == user_message.id:
        return messages

    return [*messages, user_message]

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
    if envelope.type == "assistant.list":
        yield WSEnvelope(
            type="assistant.list",
            data=[assistant.model_dump(mode="json") for assistant in list_assistants()],
        )
        return

    if envelope.type == "assistant.init":
        if envelope.data is None:
            return

        request = (
            envelope.data
            if isinstance(envelope.data, AssistantInitRequest)
            else AssistantInitRequest.model_validate(envelope.data)
        )
        assistant = initialize_assistant(
            request.chatId,
            title=request.title,
            identity_body=request.identityBody,
            heartbeat_body=request.heartbeatBody,
            memory_body=request.memoryBody,
        )
        yield WSEnvelope(
            type="assistant.init",
            data=assistant.model_dump(mode="json"),
        )
        return

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
            data=[event.model_dump(mode="json") for event in get_runs_service().resume_events(request.runId, request.afterSequence)],
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
        await get_runs_service().stop_run(stop_request.runId)
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
        next_messages = _conversation_messages_for_turn(chat)
        save_chat_session(chat.id, next_messages)
        update_chat_status(chat.id, "streaming")
        await get_runs_service().submit_run(
            RunCreateRequest(
                kind="chat",
                chatId=chat.id,
                turnId=turn_id,
            )
        )
        return
