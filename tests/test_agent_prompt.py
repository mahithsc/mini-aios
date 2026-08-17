from __future__ import annotations

from aios_core.agent_prompt import build_agent_prompt


def _prompt() -> str:
    return build_agent_prompt(
        include_subagent_tool=False,
        include_memory_tools=False,
        default_cron_timezone="UTC",
        workspace_dir="/tmp/workspace",
    )


def test_prompt_delegates_substantial_coding_to_codex() -> None:
    prompt = _prompt()

    assert "CODING DELEGATION GATE" in prompt
    assert "more than one file" in prompt
    assert "When uncertain, delegate" in prompt
    assert "Do not begin a substantial implementation yourself" in prompt
    assert "independently inspect Codex's diff" in prompt


def test_prompt_routes_app_work_to_durable_workspace() -> None:
    prompt = _prompt()

    assert "/tmp/workspace/apps/<app-id>" in prompt
    assert '"app_workspace": (' in prompt
    assert "Never fabricate replacement source" in prompt
    assert "path` set exactly to the returned `workspace_path`" in prompt


def test_prompt_waits_for_deployment_prerequisites() -> None:
    prompt = _prompt()

    assert "wait for it to become `active`" in prompt
    assert "does not make the prerequisite ready" in prompt
    assert "do not enqueue its dependents" in prompt
