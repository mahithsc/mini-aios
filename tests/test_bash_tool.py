from __future__ import annotations

import asyncio
import inspect
import os
import re
import shlex
import time
from pathlib import Path

import pytest

from aios_core.agent import (
    BASE_TOOLS,
    MAIN_TOOLS,
    create_main_agent,
    create_subagent_worker,
)
from aios_core.agent_prompt import build_agent_prompt
from aios_core.openai_runtime import as_function_tool
from aios_core.tools.shell import _run_bash, bash


PROCESS_TOOL_NAMES = {
    "process_spawn",
    "process_list",
    "process_send",
    "process_poll",
    "process_kill",
}


def _run(command: str, cwd: Path, timeout: float | None = None) -> str:
    return asyncio.run(_run_bash(command, timeout, cwd=str(cwd)))


def _tool_names(tools: list[object]) -> list[str]:
    return [
        str(getattr(tool, "name", getattr(tool, "__name__", type(tool).__name__)))
        for tool in tools
    ]


def _assert_singular_bash_surface(names: list[str]) -> None:
    assert names.count("bash") == 1
    assert PROCESS_TOOL_NAMES.isdisjoint(names)


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_until_dead(pid: int) -> bool:
    for _ in range(50):
        if not _pid_is_alive(pid):
            return True
        time.sleep(0.02)
    return False


def test_bash_has_pi_style_model_schema() -> None:
    function = as_function_tool(bash)

    assert inspect.iscoroutinefunction(bash)
    assert function.name == "bash"
    assert set(function.params_json_schema["properties"]) == {"command", "timeout"}
    assert function.params_json_schema["required"] == ["command", "timeout"]


def test_main_and_worker_expose_only_one_execution_tool() -> None:
    _assert_singular_bash_surface(_tool_names(BASE_TOOLS))
    _assert_singular_bash_surface(_tool_names(MAIN_TOOLS))
    _assert_singular_bash_surface(_tool_names(create_subagent_worker().tools))
    _assert_singular_bash_surface(_tool_names(create_main_agent().tools))


def test_prompt_documents_only_the_bash_execution_tool() -> None:
    prompt = build_agent_prompt(
        include_subagent_tool=False,
        include_memory_tools=False,
        default_cron_timezone="UTC",
        workspace_dir="/tmp/workspace",
    )

    assert '"bash": (' in prompt
    assert '{"command": "string", "timeout": "number? (seconds; no default)"}' in prompt
    assert "<process_management>" not in prompt
    for name in PROCESS_TOOL_NAMES:
        assert name not in prompt


def test_bash_combines_output_and_reports_exit_status(tmp_path: Path) -> None:
    output = _run("printf 'stdout\\n'; printf 'stderr\\n' >&2; exit 3", tmp_path)

    assert "stdout" in output
    assert "stderr" in output
    assert "exit code 3" in output


def test_bash_truncates_to_tail_and_preserves_full_log(tmp_path: Path) -> None:
    source = "for i in range(2105): print(f'line-{i}')"
    output = _run(f"python3 -c {shlex.quote(source)}", tmp_path)
    match = re.search(r"full output: ([^)\n]+)", output)

    assert match is not None
    full_output_path = Path(match.group(1))
    try:
        assert "line-0\n" not in output
        assert "line-2104" in output
        full_output = full_output_path.read_text()
        assert "line-0\n" in full_output
        assert "line-2104\n" in full_output
    finally:
        full_output_path.unlink(missing_ok=True)


@pytest.mark.parametrize(
    "timeout", [0, -1, float("inf"), float("nan"), 2_147_483.648]
)
def test_bash_rejects_invalid_timeouts(tmp_path: Path, timeout: float) -> None:
    output = _run("echo should-not-run", tmp_path, timeout)

    assert output.startswith("error: timeout must be finite")


def test_bash_timeout_kills_the_process_group(tmp_path: Path) -> None:
    pid_file = tmp_path / "timeout-child.pid"
    source = (
        "import os,time,pathlib; "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); "
        "time.sleep(60)"
    )
    output = _run(f"python3 -c {shlex.quote(source)}", tmp_path, timeout=0.25)

    assert "timed out after 0.25s" in output
    pid = int(pid_file.read_text())
    assert _wait_until_dead(pid)


def test_cancelling_bash_kills_the_process_group(tmp_path: Path) -> None:
    pid_file = tmp_path / "cancel-child.pid"
    source = (
        "import os,time,pathlib; "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); "
        "time.sleep(60)"
    )

    async def scenario() -> int:
        task = asyncio.create_task(
            _run_bash(f"python3 -c {shlex.quote(source)}", cwd=str(tmp_path))
        )
        for _ in range(100):
            if pid_file.exists():
                break
            await asyncio.sleep(0.01)
        assert pid_file.exists()
        pid = int(pid_file.read_text())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return pid

    pid = asyncio.run(scenario())
    assert _wait_until_dead(pid)


def test_quiet_inherited_pipe_does_not_hang(tmp_path: Path) -> None:
    start = time.monotonic()
    output = _run("sleep 60 & echo shell-finished", tmp_path)

    assert "shell-finished" in output
    assert time.monotonic() - start < 2
