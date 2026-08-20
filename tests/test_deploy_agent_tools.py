"""Main-agent lifecycle tools (step 6)."""

from __future__ import annotations

import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from aios_core.deploy import agent_tools
from aios_core.deploy.handoff_artifacts import ArtifactHandoffReceipt
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


def test_app_create_reserves_cloud_identity_and_creates_workspace(monkeypatch):
    monkeypatch.setattr(agent_tools, "get_current_chat_id", lambda: "chat_123")

    class FakeCloud:
        def create_app(self, name, *, idempotency_key):
            assert name == "Example"
            assert idempotency_key.startswith("main-agent-app:")
            return {"id": "app_cloud123", "name": name, "status": "active"}

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
    result = agent_tools.app_create(" Example ")

    assert result["id"] == "app_cloud123"
    assert result["app_id"] == "app_cloud123"
    assert result["name"] == "Example"
    assert result["found"] is True
    assert result["workspace_path"].endswith(result["app_id"])
    assert result["status"] == "ready"
    assert result["cloud_app"]["status"] == "active"
    assert "stubbed" not in result


def test_app_create_rejects_empty_name():
    result = agent_tools.app_create("   ")

    assert result["status"] == "error"
    assert result["error"] == "name must not be empty"


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
                "components": {"server": {"url": "https://server.example.test"}},
            }

    monkeypatch.setattr(agent_tools, "_cloud", FakeCloud)
    result = agent_tools.app_info("app_cloud123")
    assert result["app"]["id"] == "app_cloud123"
    assert result["components"]["server"]["url"] == ("https://server.example.test")


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


def _write_deployment_manifest(
    app_dir: Path,
    *,
    app_id: str = "app_cloud123",
    components: tuple[str, ...],
) -> None:
    app_dir.mkdir()
    manifest_lines = ["version: 1", f"app_id: {app_id}"]
    if "database" in components:
        (app_dir / "database" / "migrations").mkdir(parents=True)
        manifest_lines.extend(["database:", "  migrations: database/migrations"])
    if "server" in components:
        (app_dir / "server").mkdir()
        (app_dir / "server" / "Dockerfile").write_text("FROM scratch\n")
        manifest_lines.extend(
            [
                "server:",
                "  source: server",
                "  dockerfile: server/Dockerfile",
                "  health_path: /health",
            ]
        )
    if "frontend" in components:
        (app_dir / "frontend").mkdir()
        manifest_lines.extend(["frontend:", "  source: frontend"])
    (app_dir / "aios.deploy.yaml").write_text("\n".join(manifest_lines) + "\n")


def _stub_handoff(
    monkeypatch,
    workspace: Path,
    *,
    app_id: str = "app_cloud123",
    source_commit: str = "a" * 40,
    status=None,
) -> SimpleNamespace:
    record = SimpleNamespace(
        app_id=app_id,
        path=str(workspace),
        source_commit=source_commit,
        status=status or agent_tools.WorktreeStatus.HANDOFF_READY,
    )

    class Registry:
        def get_handoff(self, handoff_id):
            assert handoff_id == "handoff_123"
            return record

    monkeypatch.setattr(agent_tools, "_worktrees", Registry)
    return record


def _create_stub_artifact(tmp_path, monkeypatch, components):
    workspace = tmp_path / "worktree"
    _write_deployment_manifest(workspace, components=tuple(components))
    record = _stub_handoff(monkeypatch, workspace)

    _stub_uploaded_artifact(monkeypatch, record, components)
    return agent_tools.create_app_artifact("handoff_123")


def _stub_uploaded_artifact(monkeypatch, record, components):
    class FakeCloud:
        def prepare_app_route(self, *, app_id, artifact_id):
            return {
                "id": f"route_{artifact_id}",
                "app_id": app_id,
                "artifact_id": artifact_id,
                "hostname": "a-test.trywink.io",
                "canonical_url": "https://a-test.trywink.io",
                "api_base_url": (
                    "https://a-test.trywink.io/api"
                    if "server" in components and "frontend" in components
                    else "https://a-test.trywink.io"
                    if "server" in components
                    else None
                ),
                "routing_mode": (
                    "frontend_with_api_prefix"
                    if "server" in components and "frontend" in components
                    else "server_only"
                    if "server" in components
                    else "frontend_only"
                ),
                "routes": (
                    {"/*": "frontend", "/api/*": "server"}
                    if "server" in components and "frontend" in components
                    else {"/*": "server"}
                    if "server" in components
                    else {"/*": "frontend"}
                ),
                "cors_allowed_origins": (
                    ["https://a-test.trywink.io"]
                    if "server" in components and "frontend" in components
                    else []
                ),
                "state": "ready",
                "provisioning_status": "provisioned",
                "activation_status": "inactive",
                "live": False,
            }

        def get_app_route(self, *, app_id, route_id):
            prefix = "route_"
            if not route_id.startswith(prefix):
                raise agent_tools.CloudDeployError("route not found")
            return {
                "id": route_id,
                "app_id": app_id,
                "artifact_id": route_id[len(prefix) :],
                "state": "ready",
                "provisioning_status": "provisioned",
                "live": False,
            }

        def enqueue_pipeline(
            self, *, app_id, artifact_id, components, idempotency_key
        ):
            assert idempotency_key.startswith("main-agent:")
            return {
                "id": f"pip_{artifact_id[4:]}",
                "app_id": app_id,
                "artifact_id": artifact_id,
                "requested_components": components,
                "status": "running",
                "deployments": [
                    {
                        "id": f"dep_{component}",
                        "component": component,
                        "status": "queued" if index == 0 else "blocked",
                    }
                    for index, component in enumerate(components)
                ],
            }

        def activate_app_route(self, *, app_id, route_id, pipeline_id):
            return {
                "id": route_id,
                "app_id": app_id,
                "state": "active",
                "activation_status": "active",
                "active_pipeline_id": pipeline_id,
                "live": True,
            }

    monkeypatch.setattr(agent_tools, "_cloud", FakeCloud)

    receipt = ArtifactHandoffReceipt(
        artifact_id=f"art_{record.source_commit[:12]}",
        app_id=record.app_id,
        source_commit=record.source_commit,
        source_tree="c" * 40,
        worktree_id="wt_1234567890abcdef",
        handoff_id="handoff_123",
        inventory_sha256="d" * 64,
        archive_sha256="e" * 64,
        manifest_sha256="f" * 64,
        archive_size=100,
        file_count=3,
        total_bytes=80,
        verification_status="source_identity_validated",
        components=list(components),
        cleanup_status=agent_tools.WorktreeStatus.REMOVED,
    )

    def upload(*, registry, cloud, handoff_id):
        assert handoff_id == "handoff_123"
        return receipt

    def load(*, registry, artifact_id):
        assert artifact_id == receipt.artifact_id
        return receipt

    monkeypatch.setattr(agent_tools, "create_uploaded_artifact_from_handoff", upload)
    monkeypatch.setattr(agent_tools, "load_artifact_handoff_receipt", load)


@pytest.mark.parametrize(
    ("components", "expected"),
    [
        (("server",), ["server"]),
        (("frontend",), ["frontend"]),
        (
            ("database", "server", "frontend"),
            ["database", "server", "frontend"],
        ),
    ],
)
def test_create_app_artifact_derives_components_from_manifest(
    tmp_path, monkeypatch, components, expected
):
    workspace = tmp_path / "worktree"
    _write_deployment_manifest(workspace, components=components)
    record = _stub_handoff(monkeypatch, workspace)
    _stub_uploaded_artifact(monkeypatch, record, components)

    result = agent_tools.create_app_artifact("handoff_123")

    assert result["status"] == "ready"
    assert result["artifact_id"].startswith("art_")
    assert result["components"] == expected
    assert result["stubbed"] is False
    assert result["cleanup_status"] == "removed"
    assert result["artifact_created"] is True
    assert result["artifact_uploaded"] is True
    assert result["artifact_verified"] is True
    assert result["worktree_removed"] is True
    assert result["workspace_path"] == str(workspace)


def test_create_app_artifact_rejects_invalid_manifest(tmp_path, monkeypatch):
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    _stub_handoff(monkeypatch, workspace)

    result = agent_tools.create_app_artifact("handoff_123")

    assert result["status"] == "error"
    assert result["error_code"] == "artifact_manifest_rejected"
    assert result["error"] == "Missing aios.deploy.yaml"
    assert "Start a new Codex correction task" in result["agent_instruction"]
    assert result["retryable"] is False
    assert result["verification_status"] == "manifest_rejected"
    assert result["cleanup_status"] == "not_completed"


def test_create_app_artifact_rejects_app_id_mismatch(tmp_path, monkeypatch):
    workspace = tmp_path / "worktree"
    _write_deployment_manifest(
        workspace,
        app_id="app_different",
        components=("frontend",),
    )
    _stub_handoff(monkeypatch, workspace)

    result = agent_tools.create_app_artifact("handoff_123")

    assert result["status"] == "error"
    assert result["error_code"] == "artifact_manifest_rejected"
    assert "expected app_cloud123, found app_different" in result["error"]
    assert "Do not edit the app manifest directly" in result["agent_instruction"]
    assert result["verification_status"] == "manifest_rejected"


def test_create_app_artifact_waits_for_codex_handoff(tmp_path, monkeypatch):
    workspace = tmp_path / "not-created-yet"
    _stub_handoff(
        monkeypatch,
        workspace,
        status=agent_tools.WorktreeStatus.CODEX_ALLOCATING,
        source_commit=None,
    )

    result = agent_tools.create_app_artifact("handoff_123")

    assert result["status"] == "error"
    assert result["error_code"] == "handoff_not_ready"
    assert "Codex has not completed" in result["error"]
    assert "Wait for the Codex completion continuation" in result["agent_instruction"]
    assert result["retryable"] is True

    def missing_receipt(*, registry, artifact_id):
        raise agent_tools.WorktreeHandoffError("unknown artifact")

    monkeypatch.setattr(
        agent_tools, "load_artifact_handoff_receipt", missing_receipt
    )
    predictable_but_unissued_id = "art_predictablebutunissued"
    deployment = agent_tools.deploy_app_artifact(predictable_but_unissued_id)
    assert deployment["status"] == "error"
    assert deployment["error_code"] == "artifact_not_ready"
    assert "pipeline_id" not in deployment


def test_create_app_artifact_derives_identity_from_ready_handoff(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "worktree"
    _write_deployment_manifest(workspace, components=("server",))
    record = _stub_handoff(monkeypatch, workspace)
    _stub_uploaded_artifact(monkeypatch, record, ("server",))

    result = agent_tools.create_app_artifact("handoff_123")

    assert result["status"] == "ready"
    assert result["app_id"] == "app_cloud123"
    assert result["workspace_path"] == str(workspace)
    assert result["source_commit"] == "a" * 40


@pytest.mark.parametrize(
    ("components", "routing_mode", "routes", "api_suffix", "cors_count"),
    [
        (
            ["database", "server", "frontend"],
            "frontend_with_api_prefix",
            {"/*": "frontend", "/api/*": "server"},
            "/api",
            1,
        ),
        (["frontend"], "frontend_only", {"/*": "frontend"}, None, 0),
        (["server"], "server_only", {"/*": "server"}, "", 0),
    ],
)
def test_prepare_app_route_returns_component_specific_contract(
    tmp_path,
    monkeypatch,
    components,
    routing_mode,
    routes,
    api_suffix,
    cors_count,
):
    artifact = _create_stub_artifact(tmp_path, monkeypatch, components)
    result = agent_tools.prepare_app_route(artifact["artifact_id"])

    assert result["status"] == "ready"
    assert result["artifact_id"] == artifact["artifact_id"]
    assert result["route_id"].startswith("route_art_")
    assert result["hostname"].endswith(".trywink.io")
    assert result["canonical_url"] == f"https://{result['hostname']}"
    assert result["routing_mode"] == routing_mode
    assert result["routes"] == routes
    assert result["cors_allowed_origins"] == (
        [result["canonical_url"]] if cors_count else []
    )
    assert result["api_base_url"] == (
        None if api_suffix is None else f"{result['canonical_url']}{api_suffix}"
    )
    assert result["provisioning_status"] == "provisioned"
    assert result["live"] is False
    assert result["stubbed"] is False


def test_prepare_app_route_rejects_database_only_artifact(tmp_path, monkeypatch):
    artifact = _create_stub_artifact(tmp_path, monkeypatch, ["database"])
    result = agent_tools.prepare_app_route(artifact["artifact_id"])

    assert result["status"] == "error"
    assert "database-only" in result["error"]
    assert "route_id" not in result


def test_deploy_app_artifact_enqueues_real_cloud_pipeline(tmp_path, monkeypatch):
    artifact = _create_stub_artifact(tmp_path, monkeypatch, ["frontend", "database"])
    route = agent_tools.prepare_app_route(artifact["artifact_id"])
    result = agent_tools.deploy_app_artifact(
        artifact["artifact_id"],
        route["route_id"],
    )

    assert result["artifact_id"] == artifact["artifact_id"]
    assert result["route_id"] == route["route_id"]
    assert result["components"] == ["database", "frontend"]
    assert result["status"] == "running"
    assert result["pipeline_id"].startswith("pip_")
    assert result["stubbed"] is False
    assert [item["component"] for item in result["deployments"]] == [
        "database",
        "frontend",
    ]


def test_deploy_app_artifact_requires_route_for_public_components(
    tmp_path, monkeypatch
):
    artifact = _create_stub_artifact(tmp_path, monkeypatch, ["frontend"])
    result = agent_tools.deploy_app_artifact(artifact["artifact_id"])

    assert result["status"] == "error"
    assert result["error_code"] == "route_not_ready"
    assert "exact route_id" in result["agent_instruction"]


def test_deploy_app_artifact_allows_database_only_without_route(tmp_path, monkeypatch):
    artifact = _create_stub_artifact(tmp_path, monkeypatch, ["database"])
    result = agent_tools.deploy_app_artifact(artifact["artifact_id"])

    assert result["status"] == "running"
    assert result["route_id"] is None
    assert result["components"] == ["database"]


def test_rejected_artifact_cannot_advance_deployment_chain():
    route = agent_tools.prepare_app_route("art_invented")
    deployment = agent_tools.deploy_app_artifact("art_invented")

    assert route["status"] == "error"
    assert route["error_code"] == "artifact_not_ready"
    assert deployment["status"] == "error"
    assert deployment["error_code"] == "artifact_not_ready"
    assert "pipeline_id" not in deployment


def test_route_from_different_artifact_cannot_be_used(tmp_path, monkeypatch):
    first = _create_stub_artifact(tmp_path, monkeypatch, ["frontend"])
    route = agent_tools.prepare_app_route(first["artifact_id"])

    second_workspace = tmp_path / "second-worktree"
    _write_deployment_manifest(second_workspace, components=("frontend",))
    second_record = _stub_handoff(
        monkeypatch, second_workspace, source_commit="b" * 40
    )
    _stub_uploaded_artifact(monkeypatch, second_record, ("frontend",))
    second = agent_tools.create_app_artifact("handoff_123")
    result = agent_tools.deploy_app_artifact(second["artifact_id"], route["route_id"])

    assert result["status"] == "error"
    assert result["error_code"] == "deployment_receipt_mismatch"
    assert "different artifact" in result["error"]


def test_route_activation_and_status_use_cloud_state(
    tmp_path, monkeypatch
):
    artifact = _create_stub_artifact(tmp_path, monkeypatch, ["frontend"])
    route = agent_tools.prepare_app_route(artifact["artifact_id"])
    deployment = agent_tools.deploy_app_artifact(
        artifact["artifact_id"],
        route["route_id"],
    )
    activation = agent_tools.activate_app_route(
        "app_cloud123",
        route["route_id"],
        deployment["pipeline_id"],
    )
    status = agent_tools.app_route_status("app_cloud123", route["route_id"])

    assert activation["status"] == "active"
    assert activation["activation_status"] == "active"
    assert activation["live"] is True
    assert activation["stubbed"] is False
    assert status["status"] == "ready"
    assert status["provisioning_status"] == "provisioned"
    assert status["live"] is False
    assert status["stubbed"] is False


def test_main_agent_deployment_reads_and_rollback_use_cloud(monkeypatch):
    calls = []

    class FakeCloud:
        def check_app_status(self, app_id):
            calls.append(("app", app_id))
            return {"app_id": app_id, "overall_status": "in_process"}

        def get_deployment_pipeline(self, pipeline_id):
            calls.append(("pipeline", pipeline_id))
            return {"id": pipeline_id, "status": "running"}

        def get_deployment(self, deployment_id):
            calls.append(("deployment", deployment_id))
            return {"id": deployment_id, "status": "building", "url": None}

        def get_deployment_events(self, deployment_id, *, after):
            calls.append(("events", deployment_id, after))
            return {"events": [], "cursor": after}

        def rollback_deployment(self, deployment_id):
            calls.append(("rollback", deployment_id))
            return {"id": "dep_rollback", "status": "queued"}

    monkeypatch.setattr(agent_tools, "_cloud", FakeCloud)

    assert agent_tools.app_deployment_status("app_cloud123")["overall_status"] == (
        "in_process"
    )
    assert agent_tools.deployment_pipeline_status("pip_cloud123")["status"] == (
        "running"
    )
    assert agent_tools.deployment_status("dep_cloud123")["status"] == "building"
    assert agent_tools.deployment_events("dep_cloud123", after=4)["cursor"] == 4
    assert agent_tools.rollback_app_artifact("dep_cloud123")["status"] == "queued"
    assert calls == [
        ("app", "app_cloud123"),
        ("pipeline", "pip_cloud123"),
        ("deployment", "dep_cloud123"),
        ("events", "dep_cloud123", 4),
        ("rollback", "dep_cloud123"),
    ]


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
