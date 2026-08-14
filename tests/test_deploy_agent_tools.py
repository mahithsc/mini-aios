"""Main-agent lifecycle tools (step 6)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from aios_core.deploy import agent_tools
from aios_core.deploy.models import Project, Spec
from aios_core.deploy.store import ProjectStore
from aios_core.deploy.supervisor import Supervisor, docker_available


@pytest.fixture
def temp_store(tmp_path, monkeypatch):
    store = ProjectStore(path=tmp_path / "projects.json")
    monkeypatch.setattr(agent_tools, "_store", lambda: store)
    return store, tmp_path


def test_unknown_app_errors(temp_store):
    assert "error" in agent_tools.app_status("nope")
    assert "error" in agent_tools.app_logs("nope")
    assert "error" in agent_tools.app_stop("nope")
    assert "error" in agent_tools.app_restart("nope")


def test_apps_list(temp_store, monkeypatch):
    store, _ = temp_store
    store.save(
        Project(slug="a", source_dir=Path("/x"), spec=Spec(run=["python", "app.py"], port=8000), status="running")
    )

    class FakeSup:
        def is_running(self, project):
            return False

    monkeypatch.setattr(agent_tools, "_sup", lambda: FakeSup())
    result = agent_tools.apps_list()
    assert result["apps"] == [{"slug": "a", "status": "running", "running": False, "port": 8000}]


def _write_app(dir_path: Path):
    (dir_path / "app.py").write_text(
        textwrap.dedent(
            """
            from http.server import BaseHTTPRequestHandler, HTTPServer
            class H(BaseHTTPRequestHandler):
                def do_GET(self):
                    self.send_response(200); self.end_headers(); self.wfile.write(b"life-ok")
                def log_message(self, *a):
                    pass
            HTTPServer(("0.0.0.0", 8000), H).serve_forever()
            """
        )
    )
    (dir_path / "project.json").write_text('{"run": ["python", "app.py"], "port": 8000}')


@pytest.mark.skipif(not docker_available(), reason="Docker not available")
def test_lifecycle_status_logs_stop_restart(temp_store):
    from aios_core.deploy.deployer import deploy

    store, tmp_path = temp_store
    _write_app(tmp_path)
    assert deploy("life1", tmp_path, store=store)["status"] == "running"
    project = Project(slug="life1", source_dir=tmp_path, spec=Spec(run=["python", "app.py"], port=8000))
    try:
        assert agent_tools.app_status("life1")["running"] is True
        assert "logs" in agent_tools.app_logs("life1")

        assert agent_tools.app_stop("life1")["status"] == "stopped"
        assert agent_tools.app_status("life1")["running"] is False

        restarted = agent_tools.app_restart("life1")
        assert restarted["status"] == "running" and restarted["health_ok"] is True
        assert agent_tools.app_status("life1")["running"] is True
    finally:
        Supervisor().stop(project)
