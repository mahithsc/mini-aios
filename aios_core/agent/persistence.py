"""OpenAI Agents SDK persistence adapters for the canonical conversation store."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

from agents import RunHooks
from agents.items import TResponseInputItem
from agents.memory.session import Session
from agents.memory.session_settings import SessionSettings
from agents.tool_context import ToolContext

from ..conversation_store import (
    DEFAULT_OPENAI_HISTORY_MEDIA_CHARS,
    DEFAULT_OPENAI_HISTORY_TEXT_CHARS,
    MAIN_SCOPE,
    ConversationStore,
    _call_id,
    _item_budget,
    _item_dict,
    _item_type,
    _json_text,
    _jsonable,
)


async def _mutation(function: Any, *args: Any, **kwargs: Any) -> Any:
    """Finish each transactional store mutation before propagating cancellation."""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # SQLite writes are short and transactional. Let the commit/rollback
        # finish before propagating cancellation so callers never observe an
        # indeterminate half-batch.
        try:
            await asyncio.shield(task)
        finally:
            raise


class CanonicalConversationSession(Session):
    """OpenAI Agents Session backed by AIOS's canonical conversation rows."""

    def __init__(
        self,
        *,
        store: ConversationStore,
        chat_id: str,
        run_id: str,
        turn_id: str,
        current_user_message_id: str | None,
        current_input: TResponseInputItem | None,
        scope_key: str = MAIN_SCOPE,
        replayable: bool = True,
        session_settings: SessionSettings | None = None,
        max_history_text_chars: int = DEFAULT_OPENAI_HISTORY_TEXT_CHARS,
        max_history_media_chars: int = DEFAULT_OPENAI_HISTORY_MEDIA_CHARS,
    ) -> None:
        self.store = store
        self.chat_id = chat_id
        self.run_id = run_id
        self.turn_id = turn_id
        self.current_user_message_id = current_user_message_id
        self.current_input_json = _json_text(current_input) if current_input is not None else None
        self.scope_key = scope_key
        self.replayable = replayable
        self.session_id = f"{chat_id}:{scope_key}"
        self.session_settings = session_settings or SessionSettings()
        self.max_history_text_chars = max(0, max_history_text_chars)
        self.max_history_media_chars = max(0, max_history_media_chars)
        self._batch_index = 0
        self._initial_history_loaded = False

    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
        resolved_limit = limit if limit is not None else self.session_settings.limit
        if self.scope_key != MAIN_SCOPE:
            return []
        # The gateway has already inserted the current user row before the SDK
        # starts, so only the bootstrap read excludes this turn. Later reads
        # are used by the SDK's retry/rewind logic and must see items written
        # during the current run.
        initial_load = not self._initial_history_loaded
        exclude_turn_id = self.turn_id if initial_load else None
        if initial_load:
            current_item = (
                json.loads(self.current_input_json)
                if self.current_input_json is not None
                else None
            )
            current_text = 0
            current_media = 0
            if isinstance(current_item, dict):
                current_text, current_media = _item_budget(current_item)
            items = await asyncio.to_thread(
                self.store.list_openai_context_items,
                chat_id=self.chat_id,
                scope_key=self.scope_key,
                exclude_turn_id=exclude_turn_id,
                item_limit=resolved_limit,
                max_text_chars=max(0, self.max_history_text_chars - current_text),
                max_media_chars=max(0, self.max_history_media_chars - current_media),
            )
        else:
            # Later reads serve the SDK's persistence retry/rewind logic and
            # therefore follow the Session contract exactly rather than the
            # request-window selection used for initial model input.
            items = await asyncio.to_thread(
                self.store.list_items,
                chat_id=self.chat_id,
                scope_key=self.scope_key,
                exclude_turn_id=None,
                limit=resolved_limit,
            )
        self._initial_history_loaded = True
        return items  # type: ignore[return-value]

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        if not items:
            return
        batch_index = self._batch_index
        self._batch_index += 1
        source_message_id: str | None = None
        if self.current_input_json is not None:
            first_json = _json_text(items[0])
            if first_json == self.current_input_json:
                source_message_id = self.current_user_message_id
        await _mutation(
            self.store.append_items,
            chat_id=self.chat_id,
            scope_key=self.scope_key,
            run_id=self.run_id,
            turn_id=self.turn_id,
            items=items,
            source="sdk_session",
            replayable=self.replayable,
            source_message_id=source_message_id,
            dedupe_prefix=f"session:{batch_index}",
        )

    async def pop_item(self) -> TResponseInputItem | None:
        return await _mutation(
            self.store.pop_item,
            self.chat_id,
            self.scope_key,
        )

    async def clear_session(self) -> None:
        await _mutation(
            self.store.clear_items,
            self.chat_id,
            self.scope_key,
        )


def serialize_sdk_event(event: Any) -> tuple[str, str | None, str | None, dict[str, Any]]:
    """Return event_type, item_type, call_id and a lossless JSON-safe payload."""
    event_kind = getattr(event, "type", None) or type(event).__name__
    payload: dict[str, Any] = {"sdk_event_type": str(event_kind)}
    item_type: str | None = None
    call_id: str | None = None

    if event_kind == "raw_response_event":
        data = _jsonable(getattr(event, "data", None))
        payload["data"] = data
        if isinstance(data, dict):
            raw_type = data.get("type")
            if isinstance(raw_type, str):
                event_kind = raw_type
                item_type = raw_type
            call_id = _call_id(data)
        return str(event_kind), item_type, call_id, payload

    if event_kind == "run_item_stream_event":
        payload["name"] = getattr(event, "name", None)
        run_item = getattr(event, "item", None)
        payload["run_item_type"] = getattr(run_item, "type", None)
        raw_item = _jsonable(getattr(run_item, "raw_item", None))
        payload["raw_item"] = raw_item
        if hasattr(run_item, "output"):
            payload["output"] = _jsonable(getattr(run_item, "output", None))
        if isinstance(raw_item, dict):
            item_type = _item_type(raw_item)
            call_id = _call_id(raw_item)
        return str(getattr(event, "name", None) or event_kind), item_type, call_id, payload

    if event_kind == "agent_updated_stream_event":
        agent = getattr(event, "new_agent", None)
        payload["agent_name"] = getattr(agent, "name", None)
        return str(event_kind), None, None, payload

    payload["event"] = _jsonable(event)
    return str(event_kind), None, None, payload


class ConversationRecorder:
    """Run-scoped bridge used by hooks, tools, and stream consumers."""

    def __init__(
        self,
        *,
        store: ConversationStore,
        chat_id: str,
        run_id: str,
        turn_id: str,
        scope_key: str = MAIN_SCOPE,
    ) -> None:
        self.store = store
        self.chat_id = chat_id
        self.run_id = run_id
        self.turn_id = turn_id
        self.scope_key = scope_key

    def child(self, child_run_id: str) -> ConversationRecorder:
        return ConversationRecorder(
            store=self.store,
            chat_id=self.chat_id,
            run_id=child_run_id,
            turn_id=self.turn_id,
            scope_key=self.store.child_scope(child_run_id),
        )

    async def record_sdk_event(self, event: Any) -> int:
        event_type, item_type, call_id, payload = serialize_sdk_event(event)
        return await _mutation(
            self.store.append_event,
            chat_id=self.chat_id,
            run_id=self.run_id,
            turn_id=self.turn_id,
            scope_key=self.scope_key,
            event_type=event_type,
            item_type=item_type,
            call_id=call_id,
            payload=payload,
        )

    async def record_application_event(self, event_type: str, payload: Mapping[str, Any]) -> int:
        call_id = None
        for key in ("toolCallId", "tool_call_id", "parentToolCallId"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                call_id = value
                break
        return await _mutation(
            self.store.append_event,
            chat_id=self.chat_id,
            run_id=self.run_id,
            turn_id=self.turn_id,
            scope_key=self.scope_key,
            event_type=event_type,
            item_type="application_event",
            call_id=call_id,
            payload=payload,
        )

    async def record_custom_event(self, event: Any) -> int:
        payload = _jsonable(event)
        if not isinstance(payload, dict):
            payload = {"value": payload}
        event_type = getattr(event, "child_event_type", None) or getattr(
            event, "kind", None
        ) or type(event).__name__
        return await self.record_application_event(str(event_type), payload)

    async def persist_model_response(self, response: Any) -> set[str]:
        output = list(getattr(response, "output", None) or [])
        normalized = [_item_dict(item) for item in output]
        has_local_calls = any(_item_type(item) == "function_call" for item in normalized)
        if not has_local_calls:
            return set()
        response_id = getattr(response, "response_id", None)
        return await _mutation(
            self.store.stage_model_response,
            chat_id=self.chat_id,
            scope_key=self.scope_key,
            run_id=self.run_id,
            turn_id=self.turn_id,
            response_id=response_id if isinstance(response_id, str) else None,
            items=normalized,
            replayable=self.scope_key == MAIN_SCOPE,
        )

    async def mark_tool_started(self, call_id: str) -> None:
        await _mutation(
            self.store.mark_tool_started,
            chat_id=self.chat_id,
            scope_key=self.scope_key,
            call_id=call_id,
        )

    async def persist_tool_output(self, call_id: str, raw_item: Mapping[str, Any]) -> int:
        return await _mutation(
            self.store.complete_tool,
            chat_id=self.chat_id,
            scope_key=self.scope_key,
            run_id=self.run_id,
            turn_id=self.turn_id,
            call_id=call_id,
            raw_output_item=raw_item,
        )

    async def finalize_unfinished_tools(self) -> None:
        if self.scope_key == MAIN_SCOPE:
            await _mutation(
                self.store.finalize_turn_unfinished_tools,
                turn_id=self.turn_id,
            )
            return
        await _mutation(
            self.store.finalize_unfinished_tools,
            run_id=self.run_id,
            scope_key=self.scope_key,
        )

    async def finish_turn(
        self,
        status: str,
        payload: Mapping[str, Any],
    ) -> int:
        if self.scope_key != MAIN_SCOPE:
            raise RuntimeError("Only the main conversation scope can finish a turn")
        return await _mutation(
            self.store.finish_turn,
            turn_id=self.turn_id,
            run_id=self.run_id,
            status=status,
            payload=payload,
        )


class DurableRunHooks(RunHooks[Any]):
    """Fail-closed persistence barriers around local FunctionTool execution."""

    @staticmethod
    def _recorder(context: Any) -> ConversationRecorder | None:
        runtime_context = getattr(context, "context", None)
        recorder = getattr(runtime_context, "conversation_recorder", None)
        return recorder if isinstance(recorder, ConversationRecorder) else None

    async def on_llm_end(self, context: Any, agent: Any, response: Any) -> None:
        del agent
        recorder = self._recorder(context)
        if recorder is not None:
            # Called by the SDK after final response validation and before any
            # local tool body is invoked. A failed commit raises and therefore
            # prevents every call in this model response from starting.
            await recorder.persist_model_response(response)

    async def on_tool_start(self, context: Any, agent: Any, tool: Any) -> None:
        del agent, tool
        recorder = self._recorder(context)
        if recorder is None:
            return
        if not isinstance(context, ToolContext):
            raise TypeError("AIOS tools require a ToolContext for durable execution")
        await recorder.mark_tool_started(str(context.tool_call_id))


__all__ = [
    "CanonicalConversationSession",
    "ConversationRecorder",
    "DurableRunHooks",
    "serialize_sdk_event",
]
