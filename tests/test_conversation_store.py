from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from aios_core.agent.persistence import (
    CanonicalConversationSession,
    ConversationRecorder,
)
from aios_core.conversation_store import (
    MAIN_SCOPE,
    ConversationStore,
)
from aios_core.db import get_db_connection, initialize_app_db


@pytest.fixture
def canonical_store(tmp_path):
    db_path = tmp_path / "aios.db"
    initialize_app_db(str(db_path))
    with get_db_connection(str(db_path)) as conn:
        conn.execute(
            """
            INSERT INTO chats (id, title, status, created_at, updated_at)
            VALUES ('chat-1', 'Canonical test', 'idle', 1, 1)
            """
        )
    return ConversationStore(str(db_path)), str(db_path)


def _create_turn(
    store: ConversationStore,
    *,
    turn_id: str = "turn-1",
    user_message_id: str = "user-1",
    run_id: str = "run-1",
) -> None:
    store.create_turn(
        chat_id="chat-1",
        turn_id=turn_id,
        user_message_id=user_message_id,
        run_id=run_id,
    )
    store.ensure_seeded("chat-1", [])


def _session(
    store: ConversationStore,
    *,
    turn_id: str = "turn-1",
    user_message_id: str = "user-1",
    run_id: str = "run-1",
    current_input: dict | None = None,
) -> CanonicalConversationSession:
    return CanonicalConversationSession(
        store=store,
        chat_id="chat-1",
        run_id=run_id,
        turn_id=turn_id,
        current_user_message_id=user_message_id,
        current_input=current_input,
    )


def test_schema_migration_creates_canonical_tables_and_version(canonical_store) -> None:
    _, db_path = canonical_store

    # Migration is deliberately safe to run at every application startup.
    initialize_app_db(db_path)

    with get_db_connection(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        migration = conn.execute(
            "SELECT name, checksum FROM schema_migrations WHERE version = 3"
        ).fetchone()
        rail_migration = conn.execute(
            "SELECT name, checksum FROM schema_migrations WHERE version = 4"
        ).fetchone()
        item_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(conversation_items)")
        }
        event_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(conversation_events)")
        }

    assert {
        "conversation_threads",
        "conversation_turns",
        "conversation_items",
        "conversation_events",
        "tool_executions",
    } <= tables
    assert migration == ("canonical_conversation_storage", "conversation-v1")
    assert rail_migration == ("conversation_rail_metadata", "conversation-v2")
    assert {
        "scope_key",
        "rail",
        "position",
        "item_json",
        "dedupe_key",
        "replayable",
        "active",
    } <= item_columns
    assert {
        "scope_key",
        "rail",
        "sequence",
        "event_type",
        "provider_item_id",
        "output_index",
        "provider_sequence",
        "payload_json",
    } <= event_columns


def test_exact_openai_items_round_trip_in_replay_order(canonical_store) -> None:
    store, db_path = canonical_store
    _create_turn(store)

    user = {
        "role": "user",
        "content": [{"type": "input_text", "text": "inspect the repo"}],
    }
    reasoning = {
        "type": "reasoning",
        "id": "rs_1",
        "summary": [{"type": "summary_text", "text": "I should inspect files."}],
    }
    function_call = {
        "type": "function_call",
        "id": "fc_1",
        "call_id": "call_1",
        "name": "bash",
        "arguments": '{"command":"rg --files"}',
        "status": "completed",
    }
    function_output = {
        "type": "function_call_output",
        "call_id": "call_1",
        "output": "README.md\npyproject.toml\n",
    }
    assistant = {
        "type": "message",
        "id": "msg_1",
        "role": "assistant",
        "status": "completed",
        "content": [
            {
                "type": "output_text",
                "text": "I found the project files.",
                "annotations": [],
            }
        ],
    }
    session = _session(store, current_input=user)

    async def exercise() -> None:
        await session.add_items([user])
        staged = store.stage_model_response(
            chat_id="chat-1",
            scope_key=MAIN_SCOPE,
            run_id="run-1",
            turn_id="turn-1",
            response_id="resp_1",
            items=[reasoning, function_call],
            replayable=True,
        )
        assert staged == {"call_1"}
        store.mark_tool_started(
            chat_id="chat-1", scope_key=MAIN_SCOPE, call_id="call_1"
        )
        store.complete_tool(
            chat_id="chat-1",
            scope_key=MAIN_SCOPE,
            run_id="run-1",
            turn_id="turn-1",
            call_id="call_1",
            raw_output_item=function_output,
        )
        await session.add_items([assistant])

    asyncio.run(exercise())

    assert store.list_items(chat_id="chat-1") == [
        user,
        reasoning,
        function_call,
        function_output,
        assistant,
    ]
    with get_db_connection(db_path) as conn:
        assert [
            row[0]
            for row in conn.execute(
                """
                SELECT position FROM conversation_items
                WHERE chat_id = 'chat-1' AND scope_key = 'main'
                ORDER BY position
                """
            )
        ] == [0, 1, 2, 3, 4]


def test_parallel_tool_outputs_keep_model_call_order(canonical_store) -> None:
    store, db_path = canonical_store
    _create_turn(store)
    first_call = {
        "type": "function_call",
        "id": "fc_first",
        "call_id": "call_first",
        "name": "bash",
        "arguments": '{"command":"first"}',
    }
    second_call = {
        "type": "function_call",
        "id": "fc_second",
        "call_id": "call_second",
        "name": "bash",
        "arguments": '{"command":"second"}',
    }
    first_output = {
        "type": "function_call_output",
        "call_id": "call_first",
        "output": "first result",
    }
    second_output = {
        "type": "function_call_output",
        "call_id": "call_second",
        "output": "second result",
    }

    store.stage_model_response(
        chat_id="chat-1",
        scope_key=MAIN_SCOPE,
        run_id="run-1",
        turn_id="turn-1",
        response_id="resp_parallel",
        items=[first_call, second_call],
        replayable=True,
    )

    # Completion is intentionally the reverse of the model's call order.
    store.mark_tool_started(
        chat_id="chat-1", scope_key=MAIN_SCOPE, call_id="call_second"
    )
    store.complete_tool(
        chat_id="chat-1",
        scope_key=MAIN_SCOPE,
        run_id="run-1",
        turn_id="turn-1",
        call_id="call_second",
        raw_output_item=second_output,
    )
    store.mark_tool_started(
        chat_id="chat-1", scope_key=MAIN_SCOPE, call_id="call_first"
    )
    store.complete_tool(
        chat_id="chat-1",
        scope_key=MAIN_SCOPE,
        run_id="run-1",
        turn_id="turn-1",
        call_id="call_first",
        raw_output_item=first_output,
    )

    assert store.list_items(chat_id="chat-1") == [
        first_call,
        second_call,
        first_output,
        second_output,
    ]
    with get_db_connection(db_path) as conn:
        positions = conn.execute(
            """
            SELECT call_id, output_position FROM tool_executions
            ORDER BY response_index
            """
        ).fetchall()
    assert positions[0][1] < positions[1][1]


def test_raw_deltas_and_subagent_events_are_persisted_losslessly(
    canonical_store,
) -> None:
    store, db_path = canonical_store
    _create_turn(store)
    recorder = ConversationRecorder(
        store=store,
        chat_id="chat-1",
        run_id="run-1",
        turn_id="turn-1",
    )
    child = recorder.child("child-run-1")
    argument_delta = {
        "type": "response.function_call_arguments.delta",
        "item_id": "fc_1",
        "output_index": 0,
        "sequence_number": 7,
        "delta": '{"command":"rg',
    }
    reasoning_delta = {
        "type": "response.reasoning_summary_text.delta",
        "item_id": "rs_1",
        "output_index": 0,
        "summary_index": 0,
        "sequence_number": 8,
        "delta": "Inspecting",
    }

    async def exercise() -> None:
        await recorder.record_sdk_event(
            SimpleNamespace(type="raw_response_event", data=argument_delta)
        )
        await recorder.record_sdk_event(
            SimpleNamespace(type="raw_response_event", data=reasoning_delta)
        )
        await child.record_application_event(
            "subagent_progress",
            {
                "childRunId": "child-run-1",
                "parentToolCallId": "call_subagent",
                "message": "reading files",
            },
        )

    asyncio.run(exercise())

    with get_db_connection(db_path) as conn:
        parent_rows = conn.execute(
            """
            SELECT sequence, event_type, item_type, call_id, provider_item_id,
                   output_index, provider_sequence, payload_json
            FROM conversation_events
            WHERE run_id = 'run-1' AND scope_key = 'main'
            ORDER BY sequence
            """
        ).fetchall()
        child_row = conn.execute(
            """
            SELECT run_id, scope_key, sequence, event_type, call_id, payload_json
            FROM conversation_events WHERE run_id = 'child-run-1'
            """
        ).fetchone()

    assert [tuple(row[:7]) for row in parent_rows] == [
        (
            0,
            "response.function_call_arguments.delta",
            "response.function_call_arguments.delta",
            None,
            "fc_1",
            0,
            7,
        ),
        (
            1,
            "response.reasoning_summary_text.delta",
            "response.reasoning_summary_text.delta",
            None,
            "rs_1",
            0,
            8,
        ),
    ]
    assert json.loads(parent_rows[0][7]) == {
        "sdk_event_type": "raw_response_event",
        "data": argument_delta,
    }
    assert json.loads(parent_rows[1][7]) == {
        "sdk_event_type": "raw_response_event",
        "data": reasoning_delta,
    }
    assert child_row[:5] == (
        "child-run-1",
        "subagent:child-run-1",
        0,
        "subagent_progress",
        "call_subagent",
    )
    assert json.loads(child_row[5])["message"] == "reading files"


def test_session_excludes_current_turn_and_dedupes_retry(canonical_store) -> None:
    store, db_path = canonical_store
    historical_user = {
        "role": "user",
        "content": [{"type": "input_text", "text": "older question"}],
    }
    current_user = {
        "role": "user",
        "content": [{"type": "input_text", "text": "current question"}],
    }
    next_user = {
        "role": "user",
        "content": [{"type": "input_text", "text": "next question"}],
    }
    store.create_turn(
        chat_id="chat-1",
        turn_id="turn-1",
        user_message_id="user-1",
        run_id="run-1",
    )
    assert store.ensure_seeded("chat-1", [("user-old", historical_user)]) is True
    session = _session(store, current_input=current_user)

    async def exercise() -> None:
        await session.add_items([current_user])
        assert await session.get_items() == [historical_user]
        # ChatRunner preflights the user, then the SDK persists the same input
        # before its provider call. Both writes must target the same row.
        await session.add_items([current_user])
        assert await session.get_items() == [historical_user, current_user]

        # A recreated Session may retry the SDK's first add after a process
        # boundary; the UI message identity must make that write idempotent.
        retry_session = _session(store, current_input=current_user)
        await retry_session.add_items([current_user])
        assert await retry_session.get_items() == [historical_user]

    asyncio.run(exercise())

    with get_db_connection(db_path) as conn:
        count = conn.execute(
            """
            SELECT COUNT(*) FROM conversation_items
            WHERE source_message_id = 'user-1'
            """
        ).fetchone()[0]
    assert count == 1

    store.create_turn(
        chat_id="chat-1",
        turn_id="turn-2",
        user_message_id="user-2",
        run_id="run-2",
    )
    next_session = _session(
        store,
        turn_id="turn-2",
        user_message_id="user-2",
        run_id="run-2",
        current_input=next_user,
    )
    assert asyncio.run(next_session.get_items()) == [historical_user, current_user]


def test_openai_context_window_keeps_function_pairs_atomic(canonical_store) -> None:
    store, _ = canonical_store
    store.create_turn(
        chat_id="chat-1",
        turn_id="turn-old",
        user_message_id="user-old",
        run_id="run-old",
    )
    store.ensure_seeded("chat-1", [])
    old_user = {"role": "user", "content": "x" * 4_000}
    call = {
        "type": "function_call",
        "id": "fc-window",
        "call_id": "call-window",
        "name": "bash",
        "arguments": '{"command":"pwd"}',
    }
    output = {
        "type": "function_call_output",
        "call_id": "call-window",
        "output": "/workspace",
    }
    assistant = {
        "type": "message",
        "id": "msg-window",
        "role": "assistant",
        "content": [
            {"type": "output_text", "text": "The workspace is /workspace."}
        ],
    }
    store.append_items(
        chat_id="chat-1",
        scope_key=MAIN_SCOPE,
        run_id="run-old",
        turn_id="turn-old",
        items=[old_user, call, output, assistant],
        source="test",
        replayable=True,
    )
    current = {"role": "user", "content": "continue"}
    store.create_turn(
        chat_id="chat-1",
        turn_id="turn-current",
        user_message_id="user-current",
        run_id="run-current",
    )
    session = CanonicalConversationSession(
        store=store,
        chat_id="chat-1",
        run_id="run-current",
        turn_id="turn-current",
        current_user_message_id="user-current",
        current_input=current,
        max_history_text_chars=1_000,
    )

    assert asyncio.run(session.get_items()) == [call, output, assistant]


def test_unfinished_tools_distinguish_started_from_never_started(
    canonical_store,
) -> None:
    store, db_path = canonical_store
    _create_turn(store)
    calls = [
        {
            "type": "function_call",
            "call_id": "call_running",
            "name": "bash",
            "arguments": '{"command":"slow"}',
        },
        {
            "type": "function_call",
            "call_id": "call_pending",
            "name": "bash",
            "arguments": '{"command":"queued"}',
        },
        {
            "type": "function_call",
            "call_id": "call_completed",
            "name": "bash",
            "arguments": '{"command":"done"}',
        },
    ]
    store.stage_model_response(
        chat_id="chat-1",
        scope_key=MAIN_SCOPE,
        run_id="run-1",
        turn_id="turn-1",
        response_id="resp_cleanup",
        items=calls,
        replayable=True,
    )
    store.mark_tool_started(
        chat_id="chat-1", scope_key=MAIN_SCOPE, call_id="call_running"
    )
    store.mark_tool_started(
        chat_id="chat-1", scope_key=MAIN_SCOPE, call_id="call_completed"
    )
    store.complete_tool(
        chat_id="chat-1",
        scope_key=MAIN_SCOPE,
        run_id="run-1",
        turn_id="turn-1",
        call_id="call_completed",
        raw_output_item={
            "type": "function_call_output",
            "call_id": "call_completed",
            "output": "done",
        },
    )

    store.finalize_unfinished_tools(run_id="run-1", scope_key=MAIN_SCOPE)

    with get_db_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT call_id, status, error FROM tool_executions
            ORDER BY response_index
            """
        ).fetchall()

    assert rows == [
        (
            "call_running",
            "unknown",
            "Execution ended after the tool started without a durable result; "
            "its external effects are unknown. Do not retry it automatically.",
        ),
        (
            "call_pending",
            "cancelled",
            "Execution was cancelled before the tool started.",
        ),
        ("call_completed", "completed", None),
    ]
    replay = store.list_items(chat_id="chat-1")
    output_by_call = {
        item["call_id"]: item["output"]
        for item in replay
        if item.get("type") == "function_call_output"
    }
    assert json.loads(output_by_call["call_running"])["status"] == "unknown"
    assert json.loads(output_by_call["call_pending"])["status"] == "cancelled"
    assert output_by_call["call_completed"] == "done"


def test_child_session_rewind_cannot_modify_main_history(canonical_store) -> None:
    store, _ = canonical_store
    _create_turn(store)
    main_user = {"role": "user", "content": "main"}
    child_user = {"role": "user", "content": "child"}
    store.append_items(
        chat_id="chat-1",
        scope_key=MAIN_SCOPE,
        run_id="run-1",
        turn_id="turn-1",
        items=[main_user],
        source="test",
        replayable=True,
    )
    child_scope = store.child_scope("child-1")
    store.append_items(
        chat_id="chat-1",
        scope_key=child_scope,
        run_id="child-1",
        turn_id="turn-1",
        items=[child_user],
        source="test",
        replayable=False,
    )
    child_session = CanonicalConversationSession(
        store=store,
        chat_id="chat-1",
        run_id="child-1",
        turn_id="turn-1",
        current_user_message_id=None,
        current_input=None,
        scope_key=child_scope,
        replayable=False,
    )

    assert asyncio.run(child_session.pop_item()) == child_user
    assert store.list_items(chat_id="chat-1") == [main_user]


def test_deleting_thread_marker_invalidates_all_canonical_children(
    canonical_store,
) -> None:
    store, db_path = canonical_store
    _create_turn(store)
    call = {
        "type": "function_call",
        "call_id": "call-delete",
        "name": "bash",
        "arguments": '{"command":"true"}',
    }
    store.stage_model_response(
        chat_id="chat-1",
        scope_key=MAIN_SCOPE,
        run_id="run-1",
        turn_id="turn-1",
        response_id="response-delete",
        items=[call],
        replayable=True,
    )
    store.append_event(
        chat_id="chat-1",
        run_id="run-1",
        turn_id="turn-1",
        scope_key=MAIN_SCOPE,
        event_type="test",
        payload={"value": 1},
    )

    with get_db_connection(db_path) as conn:
        conn.execute("DELETE FROM conversation_threads WHERE chat_id = 'chat-1'")
        counts = [
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "conversation_turns",
                "conversation_items",
                "conversation_events",
                "tool_executions",
            )
        ]

    assert counts == [0, 0, 0, 0]


def test_restart_recovery_repairs_parent_and_subagent_calls(canonical_store) -> None:
    store, db_path = canonical_store
    _create_turn(store)
    parent_call = {
        "type": "function_call",
        "call_id": "call-parent-stale",
        "name": "subagent",
        "arguments": '{"task":"inspect"}',
    }
    child_call = {
        "type": "function_call",
        "call_id": "call-child-stale",
        "name": "bash",
        "arguments": '{"command":"touch external"}',
    }
    child_scope = store.child_scope("child-stale")
    store.stage_model_response(
        chat_id="chat-1",
        scope_key=MAIN_SCOPE,
        run_id="run-1",
        turn_id="turn-1",
        response_id="response-parent",
        items=[parent_call],
        replayable=True,
    )
    store.stage_model_response(
        chat_id="chat-1",
        scope_key=child_scope,
        run_id="child-stale",
        turn_id="turn-1",
        response_id="response-child",
        items=[child_call],
        replayable=False,
    )
    store.mark_tool_started(
        chat_id="chat-1", scope_key=MAIN_SCOPE, call_id="call-parent-stale"
    )
    store.mark_tool_started(
        chat_id="chat-1", scope_key=child_scope, call_id="call-child-stale"
    )
    store.set_turn_status("turn-1", "running")

    assert store.recover_stale_run("run-1", error="server restarted") is True

    with get_db_connection(db_path) as conn:
        turn_status = conn.execute(
            "SELECT status FROM conversation_turns WHERE turn_id = 'turn-1'"
        ).fetchone()[0]
        executions = conn.execute(
            "SELECT call_id, status FROM tool_executions ORDER BY call_id"
        ).fetchall()
        recovery_event = conn.execute(
            """
            SELECT event_type, payload_json FROM conversation_events
            WHERE run_id = 'run-1' ORDER BY sequence DESC LIMIT 1
            """
        ).fetchone()

    assert turn_status == "error"
    assert executions == [
        ("call-child-stale", "unknown"),
        ("call-parent-stale", "unknown"),
    ]
    assert recovery_event[0] == "run.recovered_error"
    assert json.loads(recovery_event[1])["error"] == "server restarted"
    assert [item["type"] for item in store.list_items(chat_id="chat-1")] == [
        "function_call",
        "function_call_output",
    ]


def test_parent_terminal_barrier_finalizes_every_nested_scope(canonical_store) -> None:
    store, db_path = canonical_store
    _create_turn(store)
    main_recorder = ConversationRecorder(
        store=store,
        chat_id="chat-1",
        run_id="run-1",
        turn_id="turn-1",
    )
    child_recorder = main_recorder.child("child-live")
    store.stage_model_response(
        chat_id="chat-1",
        scope_key=child_recorder.scope_key,
        run_id=child_recorder.run_id,
        turn_id="turn-1",
        response_id="response-child-live",
        items=[
            {
                "type": "function_call",
                "call_id": "call-child-live",
                "name": "bash",
                "arguments": '{"command":"touch external"}',
            }
        ],
        replayable=False,
    )
    store.mark_tool_started(
        chat_id="chat-1",
        scope_key=child_recorder.scope_key,
        call_id="call-child-live",
    )

    event_id = asyncio.run(
        main_recorder.finish_turn(
            "complete",
            {"runId": "run-1", "turnId": "turn-1"},
        )
    )
    # Retrying after a committed transaction is idempotent.
    assert (
        asyncio.run(
            main_recorder.finish_turn(
                "complete",
                {"runId": "run-1", "turnId": "turn-1"},
            )
        )
        == event_id
    )

    with get_db_connection(db_path) as conn:
        execution = conn.execute(
            """
            SELECT status, result_json FROM tool_executions
            WHERE call_id = 'call-child-live'
            """
        ).fetchone()
        turn_status = conn.execute(
            "SELECT status FROM conversation_turns WHERE turn_id = 'turn-1'"
        ).fetchone()[0]
        lifecycle_events = conn.execute(
            """
            SELECT event_type FROM conversation_events
            WHERE run_id = 'run-1' AND scope_key = 'main'
            """
        ).fetchall()
    assert execution[0] == "unknown"
    assert json.loads(execution[1])["call_id"] == "call-child-live"
    assert turn_status == "complete"
    assert lifecycle_events == [("run.completed",)]
    assert store.recover_stale_run("run-1", error="restart") is False


def test_terminal_turn_rejects_late_model_calls(canonical_store) -> None:
    store, _ = canonical_store
    _create_turn(store)
    recorder = ConversationRecorder(
        store=store,
        chat_id="chat-1",
        run_id="run-1",
        turn_id="turn-1",
    )
    asyncio.run(
        recorder.finish_turn(
            "complete",
            {"runId": "run-1", "turnId": "turn-1"},
        )
    )

    with pytest.raises(RuntimeError, match="terminal turn"):
        store.stage_model_response(
            chat_id="chat-1",
            scope_key=MAIN_SCOPE,
            run_id="run-1",
            turn_id="turn-1",
            response_id="response-late",
            items=[
                {
                    "type": "function_call",
                    "call_id": "call-late",
                    "name": "bash",
                    "arguments": '{"command":"touch late"}',
                }
            ],
            replayable=True,
        )


def test_duplicate_dispatch_cannot_resurrect_terminal_turn(canonical_store) -> None:
    store, _ = canonical_store
    _create_turn(store)
    recorder = ConversationRecorder(
        store=store,
        chat_id="chat-1",
        run_id="run-1",
        turn_id="turn-1",
    )
    asyncio.run(
        recorder.finish_turn(
            "complete",
            {"runId": "run-1", "turnId": "turn-1"},
        )
    )

    # A duplicate queue delivery resolves to the same identity, but it may
    # never reopen the completed turn or make its tools executable again.
    _create_turn(store)
    with pytest.raises(RuntimeError, match="cannot transition from complete to running"):
        store.set_turn_status("turn-1", "running")
    assert store.get_run_status("run-1") == "complete"


@pytest.mark.parametrize("terminal_status", ["complete", "error", "cancelled"])
def test_restart_recovery_repairs_tools_owned_by_terminal_turn(
    canonical_store,
    terminal_status: str,
) -> None:
    store, db_path = canonical_store
    _create_turn(store)
    child_scope = store.child_scope("child-terminal")
    store.stage_model_response(
        chat_id="chat-1",
        scope_key=child_scope,
        run_id="child-terminal",
        turn_id="turn-1",
        response_id="response-child-terminal",
        items=[
            {
                "type": "function_call",
                "call_id": "call-child-terminal",
                "name": "bash",
                "arguments": '{"command":"touch external"}',
            }
        ],
        replayable=False,
    )
    store.mark_tool_started(
        chat_id="chat-1",
        scope_key=child_scope,
        call_id="call-child-terminal",
    )
    store.set_turn_status("turn-1", terminal_status)

    assert store.recover_stale_runs(error="server restarted") == ["run-1"]

    with get_db_connection(db_path) as conn:
        execution = conn.execute(
            """
            SELECT status, result_json FROM tool_executions
            WHERE call_id = 'call-child-terminal'
            """
        ).fetchone()
        persisted_turn_status = conn.execute(
            "SELECT status FROM conversation_turns WHERE turn_id = 'turn-1'"
        ).fetchone()[0]

    assert execution[0] == "unknown"
    assert json.loads(execution[1])["type"] == "function_call_output"
    assert persisted_turn_status == terminal_status
