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


def test_apps_list_uses_local_workspace_inventory(monkeypatch):
    monkeypatch.setattr(
        agent_tools,
        "list_app_workspaces",
        lambda: {
            "apps_dir": "/workspace/apps",
            "apps": [
                {
                    "app_id": "app_cloud123",
                    "name": "Himmarshee Plastic Surgery",
                    "workspace_path": "/workspace/apps/app_cloud123",
                    "has_manifest": False,
                }
            ],
        },
    )
    result = agent_tools.apps_list()
    assert result["apps"] == [
        {
            "app_id": "app_cloud123",
            "name": "Himmarshee Plastic Surgery",
            "workspace_path": "/workspace/apps/app_cloud123",
            "has_manifest": False,
        }
    ]


def test_app_create_uses_cloud_control_plane(monkeypatch):
    class FakeCloud:
        def create_app(self, name):
            return {"id": "app_cloud123", "name": name}

    monkeypatch.setattr(agent_tools, "_cloud", FakeCloud)
    monkeypatch.setattr(
        agent_tools,
        "create_app_workspace",
        lambda app_id, name, origin_chat_id=None: {
            "app_id": app_id,
            "name": name,
            "found": True,
            "workspace_path": f"/workspace/apps/{app_id}",
        },
    )
    assert agent_tools.app_create("Example") == {
        "id": "app_cloud123",
        "app_id": "app_cloud123",
        "name": "Example",
        "found": True,
        "workspace_path": "/workspace/apps/app_cloud123",
    }


def test_app_create_returns_configuration_error(monkeypatch):
    def missing_cloud():
        raise agent_tools.CloudDeployError("AIOS_CLOUD_URL is not configured")

    monkeypatch.setattr(agent_tools, "_cloud", missing_cloud)
    assert agent_tools.app_create("Example") == {
        "error": "AIOS_CLOUD_URL is not configured"
    }


def test_app_workspace_resolves_durable_source(monkeypatch):
    monkeypatch.setattr(
        agent_tools,
        "resolve_app_workspace",
        lambda app_id, origin_chat_id=None: {
            "app_id": app_id,
            "found": True,
            "workspace_path": f"/workspace/apps/{app_id}",
        },
    )

    assert agent_tools.app_workspace("app_cloud123") == {
        "app_id": "app_cloud123",
        "found": True,
        "workspace_path": "/workspace/apps/app_cloud123",
    }


def test_app_info_uses_cloud_endpoint(monkeypatch):
    class FakeCloud:
        def get_app_info(self, app_id):
            return {
                "app": {"id": app_id},
                "components": {
                    "server": {"url": "https://server.example.test"}
                },
            }

    monkeypatch.setattr(agent_tools, "_cloud", FakeCloud)
    result = agent_tools.app_info("app_cloud123")
    assert result["app"]["id"] == "app_cloud123"
    assert result["components"]["server"]["url"] == (
        "https://server.example.test"
    )


def test_secrets_list_returns_metadata_only(monkeypatch):
    class FakeCloud:
        def list_secret_metadata(self):
            return {
                "secrets": [
                    {
                        "id": "sec_cloud123",
                        "kind": "api_key",
                        "label": "Vendor",
                        "configured": True,
                    }
                ]
            }

    monkeypatch.setattr(agent_tools, "_cloud", FakeCloud)
    result = agent_tools.secrets_list()
    assert result["secrets"][0]["id"] == "sec_cloud123"
    assert "value" not in result["secrets"][0]


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
    (dir_path / "project.json").write_text(
        '{"run": ["python", "app.py"], "port": 8000}'
    )


@pytest.mark.skipif(not docker_available(), reason="Docker not available")
def test_lifecycle_status_logs_stop_restart(temp_store):
    from aios_core.deploy.deployer import deploy

    store, tmp_path = temp_store
    _write_app(tmp_path)
    assert deploy("life1", tmp_path, store=store)["status"] == "running"
    project = Project(
        slug="life1",
        source_dir=tmp_path,
        spec=Spec(run=["python", "app.py"], port=8000),
    )
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
