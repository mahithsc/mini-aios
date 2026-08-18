from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

from aios_core.agent.prompts import build_agent_prompt


def _prompt() -> str:
    return build_agent_prompt(
        include_subagent_tool=False,
        include_memory_tools=False,
        default_cron_timezone="UTC",
        workspace_dir="/tmp/workspace",
        current_chat_id="chat-1",
        current_chat_files_dir="/tmp/workspace/session/chat-1/files",
        current_chat_artifacts_dir="/tmp/workspace/session/chat-1/artifacts",
    )


def test_prompt_exposes_one_pi_tool_and_documents_its_lifecycle() -> None:
    prompt = _prompt()

    assert prompt.count('"pi": (') == 1
    assert "start|poll|steer|stop|list" in prompt
    assert "cursor_reset=true" in prompt
    assert "status is done, error, or stopped" in prompt
    assert '"codex' not in prompt.lower()


def test_agent_registers_pi_as_the_only_external_coding_agent_tool() -> None:
    from aios_core.agent.factory import BASE_TOOLS
    from aios_core.agent.pi.tool import pi

    tool_names = [tool.__name__ for tool in BASE_TOOLS]

    assert tool_names.count("pi") == 1
    assert pi in BASE_TOOLS
    assert not {
        "codex",
        "codex_subagent",
        "codex_start",
        "codex_poll",
        "codex_stop",
    }.intersection(tool_names)


def test_tools_package_exports_pi_without_codex_lifecycle_tools() -> None:
    import aios_core.agent.tools as tools
    from aios_core.agent.pi.tool import pi

    assert tools.__getattr__("pi") is pi
    assert "pi" in tools.__all__
    assert not {
        "codex",
        "codex_subagent",
        "codex_start",
        "codex_poll",
        "codex_stop",
    }.intersection(tools.__all__)


def test_legacy_tool_module_paths_are_removed() -> None:
    assert importlib.util.find_spec("aios_core.tools") is None
    assert importlib.util.find_spec("aios_core.deploy.agent_tools") is None
    assert importlib.util.find_spec("aios_core.deploy.pi_bridge") is None


def test_runtime_shutdown_closes_pi_jobs_even_before_start(monkeypatch) -> None:
    from aios_core import initialize
    from aios_core.agent.pi import runtime as pi_job
    from aios_core.agent.tools import processes

    closed: list[str] = []
    monkeypatch.setattr(processes, "close_all_processes", lambda: closed.append("processes"))
    monkeypatch.setattr(pi_job, "close_all_pi_jobs", lambda: closed.append("pi"))
    monkeypatch.setattr(initialize, "_RUNTIME_STARTED", False)

    initialize.shutdown_runtime(stop_crons=False)

    assert closed == ["processes", "pi"]


def test_server_pi_sink_forwards_events_on_the_event_loop(monkeypatch) -> None:
    from aios_core.agent.pi import runtime as pi_job
    from server import server
    from server.gateway import bus as gateway_bus

    installed: list[object] = []
    published: list[tuple[str, str, dict]] = []

    class FakeBus:
        def publish(self, session_id: str, event_type: str, payload: dict) -> None:
            published.append((session_id, event_type, payload))

    monkeypatch.setattr(pi_job, "set_progress_sink", installed.append)
    monkeypatch.setattr(gateway_bus, "get_gateway_bus", lambda: FakeBus())

    async def install_and_emit() -> None:
        server._install_pi_progress_sink()
        sink = installed.pop()
        assert callable(sink)
        for event_type in ("pi.started", "pi.progress", "pi.completed"):
            sink("chat-1", event_type, {"job_id": "pi-1"})
        await asyncio.sleep(0)

    asyncio.run(install_and_emit())

    assert published == [
        ("chat-1", event_type, {"job_id": "pi-1"})
        for event_type in ("pi.started", "pi.progress", "pi.completed")
    ]


def test_container_pins_node_and_pi_runtime() -> None:
    dockerfile = (Path(__file__).parents[1] / "Dockerfile").read_text(encoding="utf-8")

    assert "FROM node:22.19.0-bookworm-slim AS pi-runtime" in dockerfile
    assert "@earendil-works/pi-coding-agent@0.84.2" in dockerfile
    assert "--ignore-scripts" in dockerfile
    assert "COPY --from=pi-runtime /usr/local/ /usr/local/" in dockerfile
