from __future__ import annotations

from aios_core.agent_prompt import build_agent_prompt


def _build_prompt(*, include_memory_tools: bool) -> str:
    return build_agent_prompt(
        include_subagent_tool=False,
        include_memory_tools=include_memory_tools,
        default_cron_timezone="UTC",
        workspace_dir="/tmp/workspace",
        memory_context=(
            "<memory_context>\n"
            "MEMORY (your personal notes) [1% — 10/2,200 chars]\n"
            "Project uses PostgreSQL.\n"
            "</memory_context>"
        ),
    )


def test_main_prompt_includes_memory_policy_snapshot_and_tools() -> None:
    prompt = _build_prompt(include_memory_tools=True)

    assert "<memory_policy>" in prompt
    assert "Project uses PostgreSQL" in prompt
    assert '"memory": (' in prompt
    assert '"session_search": (' in prompt
    assert "Never save passwords" in prompt


def test_worker_prompt_can_read_snapshot_without_shared_memory_tools() -> None:
    prompt = _build_prompt(include_memory_tools=False)

    assert "Project uses PostgreSQL" in prompt
    assert "<memory_policy>" not in prompt
    assert '"memory": (' not in prompt
    assert '"session_search": (' not in prompt
