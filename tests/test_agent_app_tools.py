"""Main-agent lifecycle tools (step 6)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from aios_core.agent.tools import apps
from aios_core.deploy.models import Project, Spec
from aios_core.deploy.store import ProjectStore
from aios_core.deploy.supervisor import Supervisor, docker_available


@pytest.fixture
def temp_store(tmp_path, monkeypatch):
    store = ProjectStore(path=tmp_path / "projects.json")
    monkeypatch.setattr(apps, "_store", lambda: store)
    return store, tmp_path


def test_unknown_app_errors(temp_store):
    assert "error" in apps.app_status("nope")
    assert "error" in apps.app_logs("nope")
    assert "error" in apps.app_stop("nope")
    assert "error" in apps.app_restart("nope")


def test_apps_list_uses_durable_workspace_inventory(monkeypatch):
    monkeypatch.setattr(
        apps,
        "list_app_workspaces",
        lambda: {
            "apps_dir": "/data/projects",
            "apps": [
                {
                    "app_id": "app_cloud123",
                    "name": "Example",
                    "workspace_path": "/data/projects/app_cloud123",
                    "has_manifest": False,
                }
            ],
        },
    )
    result = apps.apps_list()
    assert result["apps"][0]["app_id"] == "app_cloud123"
    assert result["apps"][0]["workspace_path"] == "/data/projects/app_cloud123"


def test_legacy_apps_list_preserves_supervisor_inventory(temp_store, monkeypatch):
    store, _ = temp_store
    store.save(
        Project(
            slug="legacy",
            source_dir=Path("/x"),
            spec=Spec(run=["python", "app.py"], port=8000),
            status="running",
        )
    )

    class FakeSup:
        def is_running(self, project):
            return False

    monkeypatch.setattr(apps, "_sup", FakeSup)
    assert apps.legacy_apps_list() == {
        "apps": [
            {
                "slug": "legacy",
                "status": "running",
                "running": False,
                "port": 8000,
            }
        ]
    }


def test_app_create_reserves_cloud_identity_and_workspace(monkeypatch):
    class FakeCloud:
        def create_app(self, name):
            return {"id": "app_cloud123", "name": name}

    monkeypatch.setattr(apps, "_cloud", FakeCloud)
    monkeypatch.setattr(
        apps,
        "create_app_workspace",
        lambda app_id, name, origin_chat_id=None: {
            "app_id": app_id,
            "name": name,
            "found": True,
            "workspace_path": f"/data/projects/{app_id}",
        },
    )

    result = apps.app_create("Example")
    assert result["app_id"] == "app_cloud123"
    assert result["workspace_path"] == "/data/projects/app_cloud123"


def test_app_workspace_resolves_durable_source(monkeypatch):
    monkeypatch.setattr(
        apps,
        "resolve_app_workspace",
        lambda app_id, origin_chat_id=None: {
            "app_id": app_id,
            "found": True,
            "workspace_path": f"/data/projects/{app_id}",
        },
    )

    assert apps.app_workspace("app_cloud123")["workspace_path"] == (
        "/data/projects/app_cloud123"
    )


def test_app_info_and_secret_metadata_use_cloud_client(monkeypatch):
    class FakeCloud:
        def get_app_info(self, app_id):
            return {"app": {"id": app_id}, "components": {}}

        def list_secret_metadata(self):
            return {
                "secrets": [
                    {
                        "id": "sec_cloud123",
                        "kind": "api_key",
                        "configured": True,
                    }
                ]
            }

    monkeypatch.setattr(apps, "_cloud", FakeCloud)
    assert apps.app_info("app_cloud123")["app"]["id"] == "app_cloud123"
    secret = apps.secrets_list()["secrets"][0]
    assert secret["id"] == "sec_cloud123"
    assert "value" not in secret


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
        assert apps.app_status("life1")["running"] is True
        assert "logs" in apps.app_logs("life1")

        assert apps.app_stop("life1")["status"] == "stopped"
        assert apps.app_status("life1")["running"] is False

        restarted = apps.app_restart("life1")
        assert restarted["status"] == "running" and restarted["health_ok"] is True
        assert apps.app_status("life1")["running"] is True
    finally:
        Supervisor().stop(project)
