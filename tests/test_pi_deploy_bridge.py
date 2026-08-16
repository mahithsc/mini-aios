from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from aios_core.deploy import pi_bridge


@pytest.mark.parametrize(
    "slug",
    ["a", "app1", "my-app", "1-app-2", "a" * 63],
)
def test_validate_slug_accepts_dns_and_docker_safe_names(slug):
    assert pi_bridge.validate_slug(slug) == slug


@pytest.mark.parametrize(
    "slug",
    ["", "A", "My-App", "-app", "app-", "app_name", "app.name", "a" * 64, "café"],
)
def test_validate_slug_rejects_unsafe_names(slug):
    with pytest.raises(pi_bridge.BridgeRequestError):
        pi_bridge.validate_slug(slug)


def test_deploy_is_locked_to_resolved_process_cwd(tmp_path, monkeypatch):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    captured = {}

    class FakeStore:
        pass

    def fake_deploy(slug, source_dir, *, store):
        captured.update(slug=slug, source_dir=source_dir, store=store)
        return {"status": "running", "url": "http://127.0.0.1:1234"}

    result = pi_bridge.deploy_from_cwd(
        "safe-app",
        _deploy_fn=fake_deploy,
        _store_factory=FakeStore,
    )

    assert captured == {
        "slug": "safe-app",
        "source_dir": project_dir.resolve(),
        "store": captured["store"],
    }
    assert isinstance(captured["store"], FakeStore)
    assert result == {
        "status": "running",
        "url": "http://127.0.0.1:1234",
        "slug": "safe-app",
        "source_dir": str(project_dir.resolve()),
        "bridge_version": 1,
    }


def test_request_protocol_has_no_source_directory_argument(capsys):
    rc = pi_bridge.main(["--slug", "safe-app", "--source-dir", "/etc"])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 2
    assert payload["status"] == "error"
    assert payload["error_code"] == "invalid_request"
    assert "usage:" in payload["error"]


def test_invalid_slug_never_calls_deployer(capsys, monkeypatch):
    def must_not_run(*_args, **_kwargs):
        raise AssertionError("deployer should not run")

    monkeypatch.setattr(pi_bridge, "_deploy", must_not_run)
    rc = pi_bridge.main(["--slug", "../escape"])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 2
    assert payload["error_code"] == "invalid_request"


def test_expected_deployment_error_is_structured_transport_success(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(pi_bridge, "ProjectStore", lambda: object())
    monkeypatch.setattr(
        pi_bridge,
        "_deploy",
        lambda slug, source_dir, *, store: {
            "status": "error",
            "error": "app unhealthy",
            "logs": "traceback",
        },
    )

    rc = pi_bridge.main(["--slug", "broken-app"])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["status"] == "error"
    assert payload["logs"] == "traceback"
    assert payload["slug"] == "broken-app"
    assert payload["source_dir"] == str(tmp_path.resolve())


def test_unexpected_bridge_failure_is_json_without_traceback(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(pi_bridge, "ProjectStore", lambda: object())

    def explode(*_args, **_kwargs):
        raise RuntimeError("host broke")

    monkeypatch.setattr(pi_bridge, "_deploy", explode)
    rc = pi_bridge.main(["--slug", "safe-app"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert rc == 1
    assert captured.err == ""
    assert payload == {
        "status": "error",
        "error_code": "bridge_failure",
        "error": "deploy bridge failed: host broke",
        "bridge_version": 1,
    }


def test_bridge_runs_as_absolute_script_from_project_cwd(tmp_path):
    bridge = Path(pi_bridge.__file__).resolve()
    result = subprocess.run(
        [sys.executable, str(bridge), "--slug", "../escape"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 2
    assert result.stderr == ""
    assert json.loads(result.stdout)["error_code"] == "invalid_request"


def test_trusted_extension_loads_in_pi_rpc(tmp_path):
    pi = shutil.which("pi")
    if pi is None:
        pytest.skip("Pi is not installed")

    extension = Path(pi_bridge.__file__).resolve().parents[1] / "pi_extensions" / "deploy.ts"
    env = dict(os.environ)
    env["PI_CODING_AGENT_DIR"] = str(tmp_path / "pi-state")
    env["PI_OFFLINE"] = "1"
    result = subprocess.run(
        [
            pi,
            "--mode",
            "rpc",
            "--no-session",
            "--offline",
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-context-files",
            "--tools",
            "deploy",
            "-e",
            str(extension),
        ],
        input='{"id":"state-1","type":"get_state"}\n',
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )

    responses = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert result.returncode == 0, result.stderr
    assert any(
        item.get("type") == "response"
        and item.get("id") == "state-1"
        and item.get("success") is True
        for item in responses
    ), (result.stdout, result.stderr)
    assert "extension" not in result.stderr.lower() or "error" not in result.stderr.lower()
