from __future__ import annotations

import io
import json
import queue
import subprocess
import threading
import time
from pathlib import Path

import pytest

from aios_core.agent.pi import runtime as pj
from aios_core.agent.pi.protocol import encode_rpc_message
from aios_core.agent.pi.runtime import (
    PiJob,
    PiJobManager,
    build_pi_command,
    resolve_pi_workdir,
    sanitized_pi_environment,
    set_progress_sink,
)

FIXTURES = Path(__file__).parent / "fixtures" / "pi_rpc"


def _fixture_events(name: str) -> list[dict]:
    return [
        json.loads(line) for line in (FIXTURES / name).read_text().splitlines() if line
    ]


class _QueueStdout:
    def __init__(self) -> None:
        self._items: queue.Queue[bytes] = queue.Queue()
        self._closed = False

    def readline(self, _limit: int = -1) -> bytes:
        return self._items.get(timeout=5)

    def push(self, message: dict) -> None:
        self._items.put(encode_rpc_message(message))

    def push_raw(self, raw: bytes) -> None:
        self._items.put(raw)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._items.put(b"")


class _ReactiveStdin:
    def __init__(self, process: _FakePiProcess) -> None:
        self.process = process
        self.buffer = bytearray()

    def write(self, data: bytes) -> int:
        assert isinstance(data, bytes), "Pi stdin must stay binary"
        self.buffer.extend(data)
        while b"\n" in self.buffer:
            raw, _, rest = self.buffer.partition(b"\n")
            self.buffer = bytearray(rest)
            self.process.handle(json.loads(raw))
        return len(data)

    def flush(self) -> None:
        return None


class _FakePiProcess:
    def __init__(
        self,
        *,
        prompt_events: list[dict] | None = None,
        final_text: str = "Task complete.",
        reject_prompt: str | None = None,
        stderr: bytes = b"",
        ignore_term: bool = False,
    ) -> None:
        self.stdout = _QueueStdout()
        self.stderr = io.BytesIO(stderr)
        self.stdin = _ReactiveStdin(self)
        self.pid = None
        self.commands: list[dict] = []
        self.prompt_events = list(prompt_events or [])
        self.final_text = final_text
        self.reject_prompt = reject_prompt
        self.ignore_term = ignore_term
        self.terminate_calls = 0
        self.kill_calls = 0
        self.returncode: int | None = None
        self._exited = threading.Event()

    def handle(self, command: dict) -> None:
        self.commands.append(command)
        command_type = command.get("type")
        request_id = command.get("id")
        if command_type == "get_state":
            self.respond(request_id, "get_state", data={"isStreaming": False})
        elif command_type == "prompt":
            if self.reject_prompt:
                self.respond(
                    request_id, "prompt", success=False, error=self.reject_prompt
                )
            else:
                self.respond(request_id, "prompt")
                for event in self.prompt_events:
                    self.stdout.push(event)
        elif command_type == "get_last_assistant_text":
            self.respond(request_id, command_type, data={"text": self.final_text})
        elif command_type == "get_session_stats":
            self.respond(request_id, command_type, data={"tokens": {"total": 42}})
        elif command_type in {"steer", "abort"}:
            self.respond(request_id, command_type)
        # extension_ui_response is intentionally not acknowledged by Pi.

    def respond(
        self,
        request_id: str,
        command: str,
        *,
        success: bool = True,
        data: dict | None = None,
        error: str | None = None,
    ) -> None:
        message = {
            "id": request_id,
            "type": "response",
            "command": command,
            "success": success,
        }
        if data is not None:
            message["data"] = data
        if error is not None:
            message["error"] = error
        self.stdout.push(message)

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if not self._exited.wait(timeout=timeout):
            raise subprocess.TimeoutExpired("pi", timeout)
        assert self.returncode is not None
        return self.returncode

    def terminate(self) -> None:
        self.terminate_calls += 1
        if not self.ignore_term:
            self.exit(-15)

    def kill(self) -> None:
        self.kill_calls += 1
        self.exit(-9)

    def send_signal(self, _signal) -> None:
        self.terminate()

    def exit(self, code: int) -> None:
        if self.returncode is None:
            self.returncode = code
            self._exited.set()
            self.stdout.close()


@pytest.fixture(autouse=True)
def _short_timeouts(monkeypatch):
    monkeypatch.setattr(pj, "HANDSHAKE_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(pj, "RPC_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(pj, "ABORT_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(pj, "STOP_GRACE_SECONDS", 0.01)
    set_progress_sink(None)
    yield
    set_progress_sink(None)


@pytest.fixture
def valid_workdir(tmp_path, monkeypatch):
    monkeypatch.setattr(pj, "resolve_pi_workdir", lambda _path: tmp_path)
    return tmp_path


def _install_process(
    monkeypatch, process: _FakePiProcess, captured: dict | None = None
) -> None:
    def popen(command, **kwargs):
        if captured is not None:
            captured["command"] = command
            captured["kwargs"] = kwargs
        return process

    monkeypatch.setattr(pj.subprocess, "Popen", popen)


def _wait_terminal(manager: PiJobManager, job_id: str, *, session_id=None, timeout=3.0):
    deadline = time.monotonic() + timeout
    result = manager.poll(job_id, wait=0.1, session_id=session_id)
    while (
        result.get("status") not in {"done", "error", "stopped"}
        and time.monotonic() < deadline
    ):
        result = manager.poll(
            job_id,
            cursor=0,
            wait=0.1,
            session_id=session_id,
        )
    return result


def test_start_handshakes_then_settled_completes_with_result(
    valid_workdir, monkeypatch
):
    process = _FakePiProcess(prompt_events=_fixture_events("success.jsonl"))
    captured: dict = {}
    _install_process(monkeypatch, process, captured)
    manager = PiJobManager()
    started = manager.start("read the project", path=".")
    result = _wait_terminal(manager, started["job_id"])

    assert result["status"] == "done"
    assert result["result"] == "Task complete."
    assert result["stats"] == {"tokens": {"total": 42}}
    assert [command["type"] for command in process.commands[:2]] == [
        "get_state",
        "prompt",
    ]
    assert captured["kwargs"]["text"] is False
    assert captured["kwargs"]["start_new_session"] is True
    kinds = [event["kind"] for event in result["events"]]
    assert "tool_start" in kinds
    assert "tool_update" in kinds
    assert "tool_end" in kinds
    assert "agent_settled" in kinds


def test_agent_end_or_message_end_without_agent_settled_stays_running(
    valid_workdir, monkeypatch
):
    events = [
        {"type": "agent_end"},
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "not settled"}],
            },
        },
    ]
    process = _FakePiProcess(prompt_events=events, final_text="now settled")
    _install_process(monkeypatch, process)
    manager = PiJobManager()
    started = manager.start("work", path=".")
    time.sleep(0.03)
    assert manager.poll(started["job_id"])["status"] == "running"

    process.stdout.push({"type": "agent_settled"})
    result = _wait_terminal(manager, started["job_id"])
    assert result["status"] == "done"
    assert result["result"] == "now settled"


def test_settled_completion_race_orders_started_before_completed(
    valid_workdir, monkeypatch
):
    process = _FakePiProcess(prompt_events=[{"type": "agent_settled"}])
    _install_process(monkeypatch, process)
    emitted: list[str] = []
    set_progress_sink(lambda _sid, event_type, _payload: emitted.append(event_type))
    manager = PiJobManager()
    started = manager.start("fast", path=".", session_id="chat-a")
    result = _wait_terminal(manager, started["job_id"], session_id="chat-a")
    assert result["status"] == "done"
    assert emitted[0] == "pi.started"
    assert emitted[-1] == "pi.completed"


def test_accepted_prompt_crash_race_replays_terminal_after_started(
    valid_workdir,
) -> None:
    emitted: list[str] = []
    set_progress_sink(lambda _sid, event_type, _payload: emitted.append(event_type))
    job = PiJob(
        "race",
        "fast",
        str(valid_workdir),
        ["pi", "--mode", "rpc"],
        owner_session_id="chat-a",
        progress_session_id="chat-a",
    )
    # Model the RPC reader observing EOF after Pi accepted the prompt response,
    # but before the start thread has recorded that acceptance.
    job._finish("error", error="Pi crashed after accepting prompt")
    assert emitted == []
    status, error, should_finalize = job._mark_prompt_accepted()
    assert (status, error, should_finalize) == (
        "error",
        "Pi crashed after accepting prompt",
        False,
    )
    assert emitted == ["pi.started", "pi.completed"]


@pytest.mark.parametrize(
    ("stop_reason", "error_message", "expected"),
    [
        ("error", "provider request failed", "provider request failed"),
        ("aborted", None, "aborted"),
    ],
)
def test_assistant_error_or_abort_settles_as_error(
    valid_workdir, monkeypatch, stop_reason, error_message, expected
):
    message = {
        "role": "assistant",
        "content": [{"type": "text", "text": "partial answer"}],
        "stopReason": stop_reason,
    }
    if error_message is not None:
        message["errorMessage"] = error_message
    process = _FakePiProcess(
        prompt_events=[
            {"type": "message_end", "message": message},
            {"type": "agent_settled"},
        ],
        final_text="partial answer",
    )
    _install_process(monkeypatch, process)
    manager = PiJobManager()
    started = manager.start("work", path=".")
    result = _wait_terminal(manager, started["job_id"])
    assert result["status"] == "error"
    assert expected in result["error"]
    assert result["result"] is None


def test_prompt_rejection_has_no_phantom_started_event_and_keeps_error_text(
    valid_workdir, monkeypatch
):
    process = _FakePiProcess(reject_prompt="model credentials missing")
    _install_process(monkeypatch, process)
    emitted: list[str] = []
    set_progress_sink(lambda _sid, event_type, _payload: emitted.append(event_type))
    result = PiJobManager().start("work", path=".", session_id="chat-a")
    assert "model credentials missing" in result["error"]
    assert emitted == []


def test_steer_is_correlated_and_chat_ownership_is_enforced(valid_workdir, monkeypatch):
    process = _FakePiProcess()
    _install_process(monkeypatch, process)
    manager = PiJobManager()
    started = manager.start("work", path=".", session_id="chat-a")
    job_id = started["job_id"]

    assert "owned" in manager.poll(job_id, session_id="chat-b")["error"]
    assert manager.list(session_id="chat-b") == {"jobs": []}
    steered = manager.steer(job_id, "focus on tests", session_id="chat-a")
    assert steered["accepted"] is True
    assert any(
        command.get("type") == "steer" and command.get("message") == "focus on tests"
        for command in process.commands
    )
    manager.stop(job_id, session_id="chat-a")


def test_extension_ui_requests_are_cancelled_fail_closed(valid_workdir, monkeypatch):
    process = _FakePiProcess(prompt_events=_fixture_events("extension_ui.jsonl"))
    _install_process(monkeypatch, process)
    manager = PiJobManager()
    started = manager.start("work", path=".")
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline and not any(
        command.get("type") == "extension_ui_response" for command in process.commands
    ):
        time.sleep(0.01)
    responses = [
        command
        for command in process.commands
        if command.get("type") == "extension_ui_response"
    ]
    assert responses == [
        {"type": "extension_ui_response", "id": "ui-1", "cancelled": True}
    ]
    polled = manager.poll(started["job_id"])
    assert any(event["kind"] == "extension_ui_cancelled" for event in polled["events"])
    manager.stop(started["job_id"])


def test_stop_sends_abort_then_term_and_kill_to_stubborn_process(
    valid_workdir, monkeypatch
):
    process = _FakePiProcess(ignore_term=True)
    _install_process(monkeypatch, process)
    manager = PiJobManager()
    started = manager.start("hang", path=".")
    stopped = manager.stop(started["job_id"])
    assert stopped["status"] == "stopped"
    assert any(command.get("type") == "abort" for command in process.commands)
    assert process.terminate_calls == 1
    assert process.kill_calls == 1


def test_independent_watchdog_stops_silent_job(valid_workdir, monkeypatch):
    process = _FakePiProcess()
    _install_process(monkeypatch, process)
    job = PiJob(
        "watchdog",
        "hang",
        str(valid_workdir),
        ["pi", "--mode", "rpc"],
        owner_session_id="default",
        safety_cap=0.04,
    )
    job.start()
    deadline = time.monotonic() + 1
    while job.current_status() == "running" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert job.poll()["status"] == "error"
    assert "safety cap" in job.poll()["error"]
    assert process.terminate_calls == 1


def test_protocol_failure_marks_error_and_reaps_process(valid_workdir, monkeypatch):
    process = _FakePiProcess()
    _install_process(monkeypatch, process)
    manager = PiJobManager()
    started = manager.start("work", path=".")
    process.stdout.push_raw(b"not-json\n")
    result = _wait_terminal(manager, started["job_id"])
    assert result["status"] == "error"
    assert "valid JSON" in result["error"]
    assert process.terminate_calls == 1


def test_absolute_cursor_resets_after_bounded_event_trimming(
    valid_workdir, monkeypatch
):
    process = _FakePiProcess()
    _install_process(monkeypatch, process)
    job = PiJob(
        "bounded",
        "work",
        str(valid_workdir),
        ["pi", "--mode", "rpc"],
        owner_session_id="default",
        max_events=3,
    )
    job.start()
    for index in range(6):
        process.stdout.push(
            {
                "type": "tool_execution_start",
                "toolCallId": str(index),
                "toolName": "read",
                "args": {"path": str(index)},
            }
        )
    deadline = time.monotonic() + 1
    while job.poll()["cursor"] < 6 and time.monotonic() < deadline:
        time.sleep(0.01)
    result = job.poll(cursor=0)
    assert result["cursor"] == 6
    assert result["buffer_start_cursor"] == 3
    assert result["cursor_reset"] is True
    assert len(result["events"]) == 3
    job.stop()


def test_tool_update_storm_is_coalesced_and_progress_is_throttled(
    valid_workdir, monkeypatch
):
    monkeypatch.setattr(pj, "TOOL_UPDATE_COALESCE_SECONDS", 60.0)
    process = _FakePiProcess()
    _install_process(monkeypatch, process)
    emitted: list[tuple[str, dict]] = []
    set_progress_sink(
        lambda _sid, event_type, payload: emitted.append((event_type, payload))
    )
    manager = PiJobManager()
    started = manager.start("work", path=".", session_id="chat-a")
    job = manager._jobs[started["job_id"]]
    for index in range(100):
        process.stdout.push(
            {
                "type": "tool_execution_update",
                "toolCallId": "chatty-tool",
                "toolName": "bash",
                "partialResult": {"content": [{"type": "text", "text": str(index)}]},
            }
        )
    deadline = time.monotonic() + 1
    while job._next_event_cursor < 100 and time.monotonic() < deadline:
        time.sleep(0.01)

    result = manager.poll(started["job_id"], cursor=0, session_id="chat-a")
    assert result["latest_cursor"] == 100
    assert result["cursor_reset"] is True
    assert len(result["events"]) == 1
    assert result["events"][0]["output"]["content"][0]["text"] == "99"
    progress_updates = [
        payload
        for event_type, payload in emitted
        if event_type == "pi.progress" and payload.get("kind") == "tool_update"
    ]
    assert len(progress_updates) == 1
    manager.stop(started["job_id"], session_id="chat-a")


def test_poll_pages_events_by_count(valid_workdir, monkeypatch):
    monkeypatch.setattr(pj, "MAX_POLL_EVENTS", 3)
    monkeypatch.setattr(pj, "MAX_POLL_BYTES", 1_000_000)
    process = _FakePiProcess()
    _install_process(monkeypatch, process)
    manager = PiJobManager()
    started = manager.start("work", path=".")
    job = manager._jobs[started["job_id"]]
    for index in range(8):
        process.stdout.push(
            {
                "type": "tool_execution_start",
                "toolCallId": str(index),
                "toolName": "read",
                "args": {"path": str(index)},
            }
        )
    deadline = time.monotonic() + 1
    while job._next_event_cursor < 8 and time.monotonic() < deadline:
        time.sleep(0.01)

    first = manager.poll(started["job_id"], cursor=0)
    second = manager.poll(started["job_id"], cursor=first["cursor"])
    third = manager.poll(started["job_id"], cursor=second["cursor"])
    assert [len(first["events"]), len(second["events"]), len(third["events"])] == [
        3,
        3,
        2,
    ]
    assert first["has_more"] is True
    assert second["has_more"] is True
    assert third["has_more"] is False
    assert third["cursor"] == third["latest_cursor"] == 8
    manager.stop(started["job_id"])


def test_poll_and_retained_events_are_bounded_by_bytes(valid_workdir, monkeypatch):
    monkeypatch.setattr(pj, "MAX_POLL_EVENTS", 100)
    monkeypatch.setattr(pj, "MAX_POLL_BYTES", 500)
    monkeypatch.setattr(pj, "MAX_EVENT_BUFFER_BYTES", 900)
    process = _FakePiProcess()
    _install_process(monkeypatch, process)
    manager = PiJobManager()
    started = manager.start("work", path=".")
    job = manager._jobs[started["job_id"]]
    for index in range(4):
        process.stdout.push(
            {
                "type": "tool_execution_start",
                "toolCallId": str(index),
                "toolName": "read",
                "args": {"path": f"{index}-" + ("x" * 600)},
            }
        )
    deadline = time.monotonic() + 1
    while job._next_event_cursor < 4 and time.monotonic() < deadline:
        time.sleep(0.01)

    result = manager.poll(started["job_id"], cursor=0)
    # The buffer always preserves at least its latest event, even when one
    # useful event is itself larger than the configured page byte target.
    assert len(result["events"]) == 1
    assert result["cursor_reset"] is True
    assert result["buffer_start_cursor"] == 3
    assert job._event_bytes <= 900
    assert (
        sum(
            len(json.dumps(event, separators=(",", ":")).encode())
            for event in result["events"]
        )
        <= 500
    )
    assert "payload omitted" in result["events"][0]["detail"]
    manager.stop(started["job_id"])


def test_concurrency_cap_reserves_active_slot(valid_workdir, monkeypatch):
    process = _FakePiProcess()
    calls = 0

    def popen(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return process

    monkeypatch.setattr(pj.subprocess, "Popen", popen)
    manager = PiJobManager(max_active_jobs=1)
    first = manager.start("first", path=".")
    second = manager.start("second", path=".")
    assert "job_id" in first
    assert "too many active" in second["error"]
    assert calls == 1
    manager.stop(first["job_id"])


def test_stderr_buffer_is_bounded(valid_workdir, monkeypatch):
    monkeypatch.setattr(pj, "MAX_STDERR_BYTES", 1024)
    process = _FakePiProcess(stderr=b"x" * 10_000)
    _install_process(monkeypatch, process)
    manager = PiJobManager()
    started = manager.start("work", path=".")
    job = manager._jobs[started["job_id"]]
    deadline = time.monotonic() + 1
    while len(job._stderr) < 1024 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(job._stderr) == 1024
    manager.stop(started["job_id"])


def test_command_disables_discovery_and_enforces_profiles(tmp_path) -> None:
    extension = tmp_path / "deploy.ts"
    extension.write_text("export default {}")
    coding = build_pi_command(profile="coding", deploy_extension=extension)
    read_only = build_pi_command(profile="read_only", deploy_extension=extension)
    assert coding[:4] == ["pi", "--mode", "rpc", "--no-session"]
    assert "--no-extensions" in coding
    assert "--no-context-files" in coding
    assert coding[coding.index("--tools") + 1] == (
        "read,grep,find,ls,bash,edit,write,deploy,deployment_status,"
        "get_deployment_status,get_deployment_events,get_app_info,"
        "check_app_status,cancel_cloud_deployment,resume_cloud_deployment,"
        "rollback_cloud_deployment,upload_app_media,list_app_media,"
        "get_app_media_url,delete_app_media,list_database_tables,"
        "inspect_database_table,query_database_table,list_database_migrations"
    )
    assert "--extension" in coding
    assert read_only[read_only.index("--tools") + 1] == "read,grep,find,ls"
    assert "--extension" not in read_only


def test_default_deploy_extension_lives_with_pi_runtime() -> None:
    assert pj._DEPLOY_EXTENSION == (
        Path(pj.__file__).resolve().parent / "extensions" / "deploy.ts"
    )
    assert pj._DEPLOY_EXTENSION.is_file()


def test_environment_is_minimal_but_keeps_provider_auth() -> None:
    env = sanitized_pi_environment(
        {
            "PATH": "/bin",
            "HOME": "/home/pi",
            "OPENAI_API_KEY": "model-secret",
            "APP_ENV": "production",
            "AIOS_ENV": "prod",
            "ENV": "production",
            "AIOS_DATA_DIR": "/var/lib/mini-aios",
            "AIOS_CLOUD_DEVICE_TOKEN": "device-control-plane-token",
            "DATABASE_URL": "must-not-leak",
            "RANDOM_APP_SECRET": "must-not-leak",
        }
    )
    assert env["OPENAI_API_KEY"] == "model-secret"
    assert env["APP_ENV"] == "production"
    assert env["AIOS_ENV"] == "prod"
    assert env["ENV"] == "production"
    assert env["AIOS_DATA_DIR"] == "/var/lib/mini-aios"
    assert env["AIOS_CLOUD_DEVICE_TOKEN"] == "device-control-plane-token"
    assert env["AIOS_PYTHON"]
    assert "DATABASE_URL" not in env
    assert "RANDOM_APP_SECRET" not in env


def test_workdir_rejects_symlink_escape(tmp_path, monkeypatch) -> None:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    link = allowed / "escape"
    link.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(pj, "resolve_agent_path", lambda _path: link)
    monkeypatch.setattr(pj, "get_current_chat_scratch_dir", lambda: allowed)
    monkeypatch.setattr(pj, "get_data_dir", lambda: allowed)
    monkeypatch.delenv("AIOS_PI_ALLOWED_ROOTS", raising=False)
    with pytest.raises(ValueError, match="escapes allowed Pi roots"):
        resolve_pi_workdir("escape")


def test_finished_job_retention_is_bounded(monkeypatch) -> None:
    monkeypatch.setattr(pj, "MAX_JOB_RECORDS", 2)
    manager = PiJobManager()
    for index in range(3):
        job = PiJob(
            str(index),
            "done",
            "/tmp",
            ["pi"],
            owner_session_id="default",
        )
        job._finish("done", result="ok")
        manager._jobs[job.id] = job
        time.sleep(0.001)
    listed = manager.list()["jobs"]
    assert [job["job_id"] for job in listed] == ["1", "2"]
