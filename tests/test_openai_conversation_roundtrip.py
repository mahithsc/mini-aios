from __future__ import annotations

import asyncio
import json

from agents import Agent, RunConfig, Runner
from agents.testing import ScriptedModel, assistant_message, function_call
from openai.types.responses import ResponseReasoningItem

from aios_core.conversation_store import (
    CanonicalConversationSession,
    ConversationRecorder,
    ConversationStore,
    DurableRunHooks,
)
from aios_core.db import get_db_connection, initialize_app_db
from aios_core.openai_runtime import AgentRuntimeContext, as_function_tool


def _user(text: str) -> dict:
    return {
        "role": "user",
        "content": [{"type": "input_text", "text": text}],
    }


def _session(
    store: ConversationStore,
    *,
    run_id: str,
    turn_id: str,
    user_message_id: str,
    current_input: dict,
) -> CanonicalConversationSession:
    store.create_turn(
        chat_id="chat-roundtrip",
        turn_id=turn_id,
        user_message_id=user_message_id,
        run_id=run_id,
    )
    return CanonicalConversationSession(
        store=store,
        chat_id="chat-roundtrip",
        run_id=run_id,
        turn_id=turn_id,
        current_user_message_id=user_message_id,
        current_input=current_input,
    )


def test_two_turn_runner_round_trip_reconstructs_openai_function_call_history(
    tmp_path,
) -> None:
    """Exercise the real Agents SDK run loop without making a network request.

    The first application turn takes two model steps: a function call followed
    by a final assistant message. The second application turn must be rebuilt
    solely from the canonical SQLite session and presented to the model as
    ordered OpenAI Responses items.
    """

    db_path = str(tmp_path / "roundtrip.db")
    initialize_app_db(db_path)
    with get_db_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO chats (id, title, status, created_at, updated_at)
            VALUES ('chat-roundtrip', 'SDK round trip', 'idle', 1, 1)
            """
        )

    store = ConversationStore(db_path)
    store.ensure_seeded("chat-roundtrip", [])
    first_user = _user("list the files")
    second_user = _user("what did the command return?")
    tool_invocations: list[str] = []

    def bash(command: str) -> str:
        tool_invocations.append(command)
        return "README.md\npyproject.toml\n"

    reasoning = ResponseReasoningItem(
        id="reasoning-1",
        type="reasoning",
        status="completed",
        summary=[],
        encrypted_content="opaque-reasoning-payload",
    )
    model = ScriptedModel(
        [
            {
                "response_id": "response-tool-call",
                "output": [
                    reasoning,
                    function_call(
                        "bash",
                        {"command": "rg --files"},
                        call_id="call-bash-1",
                        item_id="function-call-1",
                    ),
                ],
            },
            {
                "response_id": "response-first-answer",
                "output": [
                    assistant_message(
                        "The command returned two files.",
                        item_id="assistant-1",
                    )
                ],
            },
            {
                "response_id": "response-second-answer",
                "output": [
                    assistant_message(
                        "It returned README.md and pyproject.toml.",
                        item_id="assistant-2",
                    )
                ],
            },
        ]
    )
    agent = Agent(
        name="roundtrip-test",
        model=model,
        tools=[as_function_tool(bash)],
    )

    async def run_turns() -> None:
        first_session = _session(
            store,
            run_id="run-1",
            turn_id="turn-1",
            user_message_id="user-1",
            current_input=first_user,
        )
        first_recorder = ConversationRecorder(
            store=store,
            chat_id="chat-roundtrip",
            run_id="run-1",
            turn_id="turn-1",
        )
        await first_session.add_items([first_user])
        first_result = Runner.run_streamed(
            agent,
            input=[first_user],
            context=AgentRuntimeContext(conversation_recorder=first_recorder),
            hooks=DurableRunHooks(),
            session=first_session,
            run_config=RunConfig(tracing_disabled=True),
        )
        async for _event in first_result.stream_events():
            pass
        assert first_result.final_output == "The command returned two files."

        second_session = _session(
            store,
            run_id="run-2",
            turn_id="turn-2",
            user_message_id="user-2",
            current_input=second_user,
        )
        second_recorder = ConversationRecorder(
            store=store,
            chat_id="chat-roundtrip",
            run_id="run-2",
            turn_id="turn-2",
        )
        await second_session.add_items([second_user])
        second_result = Runner.run_streamed(
            agent,
            input=[second_user],
            context=AgentRuntimeContext(conversation_recorder=second_recorder),
            hooks=DurableRunHooks(),
            session=second_session,
            run_config=RunConfig(tracing_disabled=True),
        )
        async for _event in second_result.stream_events():
            pass
        assert second_result.final_output == "It returned README.md and pyproject.toml."

    asyncio.run(run_turns())
    model.assert_complete()
    assert all(call.streamed for call in model.calls)
    assert tool_invocations == ["rg --files"]

    # The third provider call is the first (and only) model call of the second
    # application turn. Its history came from CanonicalConversationSession.
    second_turn_input = model.calls[2].input
    assert isinstance(second_turn_input, list)
    assert [item.get("type", "message") for item in second_turn_input] == [
        "message",
        "reasoning",
        "function_call",
        "function_call_output",
        "message",
        "message",
    ]
    assert second_turn_input[0] == first_user
    assert second_turn_input[2]["call_id"] == "call-bash-1"
    assert second_turn_input[3] == {
        "type": "function_call_output",
        "call_id": "call-bash-1",
        "output": "README.md\npyproject.toml\n",
    }
    assert second_turn_input[-1] == second_user

    assistant_items = [
        item for item in second_turn_input if item.get("role") == "assistant"
    ]
    assert len(assistant_items) == 1
    assert [part["type"] for part in assistant_items[0]["content"]] == ["output_text"]
    assert all(
        part.get("type") != "input_text"
        for item in assistant_items
        for part in item.get("content", [])
    )

    # These rows prove that the fail-closed LLM hook staged the call before
    # execution and the FunctionTool custom-data extractor committed the exact
    # SDK-generated output item after execution.
    with get_db_connection(db_path) as conn:
        execution = conn.execute(
            """
            SELECT status, response_id, result_json
            FROM tool_executions
            WHERE call_id = 'call-bash-1'
            """
        ).fetchone()

    assert execution[:2] == ("completed", "response-tool-call")
    assert json.loads(execution[2]) == second_turn_input[3]
