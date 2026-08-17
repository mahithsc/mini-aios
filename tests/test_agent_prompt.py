from __future__ import annotations

from aios_core.agent_prompt import build_agent_prompt


def _prompt() -> str:
    return build_agent_prompt(
        include_subagent_tool=False,
        include_memory_tools=False,
        default_cron_timezone="UTC",
        workspace_dir="/tmp/workspace",
    )


def test_prompt_routes_every_code_or_app_task_to_codex() -> None:
    prompt = _prompt()

    assert "HARD CODEX ROUTING GATE — NON-OPTIONAL" in prompt
    assert "even remotely about code" in prompt
    assert "must not substitute `glob`, `grep`, `read`, `bash`" in prompt
    assert "call `codex_start` in that same turn" in prompt
    assert "explicit Codex request overrides" in prompt
    assert "including incomplete device-only apps" in prompt
    assert "Do not use the cloud app inventory" in prompt
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
