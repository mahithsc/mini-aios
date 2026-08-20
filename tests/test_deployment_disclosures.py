import asyncio
from types import SimpleNamespace

from agno.agent import RunEvent as AgentRunEvent

from aios_core.deploy.disclosures import (
    STUB_DEPLOYMENT_DISCLOSURE,
    missing_disclosure_suffix,
    required_disclosures_from_tool_result,
    stub_deployment_evidence,
)
from server.execution.runners import chat as chat_runner
from server.types.run import Run


def test_stub_evidence_explicitly_denies_unsupported_claims():
    evidence = stub_deployment_evidence(worktree_path="/workspace/wt_1")

    assert evidence == {
        "artifact_created": False,
        "artifact_uploaded": False,
        "artifact_verified": False,
        "worktree_removed": False,
        "deployment_performed": False,
        "route_live": False,
        "required_disclosure": STUB_DEPLOYMENT_DISCLOSURE,
        "worktree_path": "/workspace/wt_1",
    }


def test_disclosure_is_extracted_from_dict_or_serialized_tool_result():
    payload = {"required_disclosure": STUB_DEPLOYMENT_DISCLOSURE}

    assert required_disclosures_from_tool_result(payload) == [
        STUB_DEPLOYMENT_DISCLOSURE
    ]
    assert required_disclosures_from_tool_result(str(payload)) == [
        STUB_DEPLOYMENT_DISCLOSURE
    ]


def test_runtime_appends_missing_disclosure_exactly_once():
    suffix = missing_disclosure_suffix(
        "The deployment simulation completed.", [STUB_DEPLOYMENT_DISCLOSURE]
    )

    assert suffix == f"\n\n{STUB_DEPLOYMENT_DISCLOSURE}"
    assert (
        missing_disclosure_suffix(
            f"Summary. {STUB_DEPLOYMENT_DISCLOSURE}",
            [STUB_DEPLOYMENT_DISCLOSURE],
        )
        == ""
    )


def test_chat_runner_streams_and_persists_required_disclosure(monkeypatch):
    class FakeAgent:
        async def arun(self, messages, stream, stream_events):
            yield SimpleNamespace(
                event=AgentRunEvent.tool_call_completed,
                tool=SimpleNamespace(
                    tool_name="app_route_status",
                    tool_call_id="tool_1",
                    result=str({"required_disclosure": STUB_DEPLOYMENT_DISCLOSURE}),
                ),
            )
            yield SimpleNamespace(
                event=AgentRunEvent.run_content,
                content="The simulation completed.",
            )

    class FakeRunsService:
        def __init__(self):
            self.events = []

        async def emit_event(self, run_id, event):
            self.events.append(event)
            return event

    async def set_mode(mode):
        return None

    monkeypatch.setattr(chat_runner, "create_agent", lambda chat_id: FakeAgent())
    monkeypatch.setattr(chat_runner, "load_chat_session", lambda chat_id: [])
    monkeypatch.setattr(chat_runner, "lights", SimpleNamespace(set_mode=set_mode))
    service = FakeRunsService()
    run = Run(
        id="run_1",
        kind="chat",
        status="running",
        createdAt=1,
        updatedAt=1,
        chatId="chat_1",
    )

    asyncio.run(chat_runner.ChatRunner().execute(run, service))

    tokens = [
        event.event.data["value"]
        for event in service.events
        if event.event.type == "token"
    ]
    assert tokens == [
        "The simulation completed.",
        f"\n\n{STUB_DEPLOYMENT_DISCLOSURE}",
    ]
    assert service.events[-1].event.type == "completed"
