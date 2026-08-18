from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from aios_core.agent.pi import cloud_bridge

CLOUD_TOOL_NAMES = (
    "deploy",
    "deployment_status",
    "get_deployment_status",
    "get_deployment_events",
    "get_app_info",
    "check_app_status",
    "cancel_cloud_deployment",
    "resume_cloud_deployment",
    "rollback_cloud_deployment",
    "upload_app_media",
    "list_app_media",
    "get_app_media_url",
    "delete_app_media",
    "list_database_tables",
    "inspect_database_table",
    "query_database_table",
    "list_database_migrations",
)


def _write_manifest(root: Path, *, app_id: str = "app_cloud123") -> None:
    server = root / "server"
    server.mkdir(parents=True, exist_ok=True)
    (server / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (root / "aios.deploy.yaml").write_text(
        f"version: 1\napp_id: {app_id}\nserver: {{}}\n",
        encoding="utf-8",
    )
    (root / ".aios-app.json").write_text(
        json.dumps({"version": 1, "app_id": app_id, "name": "Cloud app"}),
        encoding="utf-8",
    )


def test_components_are_deduplicated_and_put_in_dependency_order() -> None:
    assert cloud_bridge.normalize_components(["frontend", "database", "frontend"]) == [
        "database",
        "frontend",
    ]
    assert cloud_bridge.normalize_components(None) is None


@pytest.mark.parametrize("components", [[], ["worker"], ["server", "unknown"]])
def test_invalid_components_are_rejected(components) -> None:
    with pytest.raises(cloud_bridge.BridgeRequestError):
        cloud_bridge.normalize_components(components)


def test_deploy_is_manifest_rooted_ordered_and_idempotent(tmp_path) -> None:
    app_root = tmp_path / "app"
    nested = app_root / "server" / "src"
    nested.mkdir(parents=True)
    _write_manifest(app_root)
    captured = {}

    class FakeCloud:
        def deploy_pipeline(
            self, app_dir, *, components, idempotency_key
        ) -> dict[str, object]:
            captured.update(
                app_dir=app_dir,
                components=components,
                idempotency_key=idempotency_key,
            )
            return {"id": "pip_cloud123", "status": "running"}

    result = cloud_bridge.deploy_from_cwd(
        operation_id="pi-operation123",
        components=["frontend", "database"],
        _cwd=nested,
        _client_factory=FakeCloud,
    )

    assert captured == {
        "app_dir": app_root.resolve(),
        "components": ["database", "frontend"],
        "idempotency_key": "pi-operation123",
    }
    assert result == {
        "id": "pip_cloud123",
        "status": "running",
        "source_dir": str(app_root.resolve()),
        "bridge_version": 3,
    }


def test_deploy_requires_manifest_in_current_directory_tree(tmp_path) -> None:
    with pytest.raises(cloud_bridge.BridgeRequestError, match="aios.deploy.yaml"):
        cloud_bridge.deploy_from_cwd(
            operation_id="pi-operation123",
            _cwd=tmp_path,
            _client_factory=lambda: object(),
        )


def test_deploy_rejects_workspace_identity_mismatch(tmp_path) -> None:
    app_root = tmp_path / "app_cloud123"
    app_root.mkdir()
    _write_manifest(app_root)
    (app_root / ".aios-app.json").write_text(
        json.dumps({"version": 1, "app_id": "app_other123"}),
        encoding="utf-8",
    )

    with pytest.raises(cloud_bridge.BridgeRequestError, match="must match"):
        cloud_bridge.deploy_from_cwd(
            operation_id="pi-operation123",
            _cwd=app_root,
            _client_factory=lambda: object(),
        )


def test_pipeline_and_component_status_use_validated_ids() -> None:
    class FakeCloud:
        def get_deployment_pipeline(self, pipeline_id):
            return {"id": pipeline_id, "status": "completed"}

        def get_deployment(self, deployment_id):
            return {"id": deployment_id, "status": "active"}

        def get_deployment_events(self, deployment_id, *, after):
            return {"events": [], "cursor": after, "deployment": deployment_id}

    cloud = FakeCloud()
    pipeline = cloud_bridge.get_pipeline_status(
        "pip_cloud123", _client_factory=lambda: cloud
    )
    deployment = cloud_bridge.get_deployment_status(
        "dep_cloud123", _client_factory=lambda: cloud
    )
    events = cloud_bridge.get_deployment_events(
        "dep_cloud123", after=4, _client_factory=lambda: cloud
    )

    assert pipeline["status"] == "completed"
    assert deployment["status"] == "active"
    assert events == {
        "events": [],
        "cursor": 4,
        "deployment": "dep_cloud123",
        "status": "ok",
        "deployment_id": "dep_cloud123",
        "bridge_version": 3,
    }


def test_app_info_status_and_deployment_controls_use_cloud_client() -> None:
    calls: list[tuple[str, str]] = []

    class FakeCloud:
        def get_app_info(self, app_id):
            calls.append(("info", app_id))
            return {"app": {"id": app_id}}

        def check_app_status(self, app_id):
            calls.append(("check", app_id))
            return {"overall_status": "queued"}

        def cancel_deployment(self, deployment_id):
            calls.append(("cancel", deployment_id))
            return {"status": "cancelled"}

        def resume_deployment(self, deployment_id):
            calls.append(("resume", deployment_id))
            return {"status": "queued"}

        def rollback_deployment(self, deployment_id):
            calls.append(("rollback", deployment_id))
            return {"status": "queued"}

    cloud = FakeCloud()

    def factory():
        return cloud

    assert (
        cloud_bridge.get_app_info("app_cloud123", _client_factory=factory)["status"]
        == "ok"
    )
    assert (
        cloud_bridge.check_app_status("app_cloud123", _client_factory=factory)[
            "overall_status"
        ]
        == "queued"
    )
    assert (
        cloud_bridge.cancel_deployment("dep_cloud123", _client_factory=factory)["status"]
        == "cancelled"
    )
    cloud_bridge.resume_deployment("dep_cloud123", _client_factory=factory)
    cloud_bridge.rollback_deployment("dep_cloud123", _client_factory=factory)

    assert calls == [
        ("info", "app_cloud123"),
        ("check", "app_cloud123"),
        ("cancel", "dep_cloud123"),
        ("resume", "dep_cloud123"),
        ("rollback", "dep_cloud123"),
    ]


def test_media_upload_is_locked_to_current_manifest_workspace(tmp_path) -> None:
    app_root = tmp_path / "app"
    nested = app_root / "server" / "src"
    nested.mkdir(parents=True)
    _write_manifest(app_root)
    media = app_root / "hero.png"
    media.write_bytes(b"png")
    captured = {}

    class FakeCloud:
        def upload_app_media(
            self,
            app_id,
            local_path,
            *,
            destination,
            content_type,
            allowed_root,
        ):
            captured.update(
                app_id=app_id,
                local_path=local_path,
                destination=destination,
                content_type=content_type,
                allowed_root=allowed_root,
            )
            return {"id": "med_cloud123", "status": "ready"}

    result = cloud_bridge.upload_app_media(
        "app_cloud123",
        str(media),
        destination="images/hero.png",
        content_type="IMAGE/PNG",
        _cwd=nested,
        _client_factory=FakeCloud,
    )

    assert captured == {
        "app_id": "app_cloud123",
        "local_path": str(media),
        "destination": "images/hero.png",
        "content_type": "image/png",
        "allowed_root": app_root.resolve(),
    }
    assert result["status"] == "ready"


def test_media_upload_rejects_app_mismatch_and_unsafe_destination(tmp_path) -> None:
    _write_manifest(tmp_path)
    media = tmp_path / "hero.png"
    media.write_bytes(b"png")
    outside = tmp_path.parent / f"{tmp_path.name}-outside.png"
    outside.write_bytes(b"png")

    with pytest.raises(cloud_bridge.BridgeRequestError, match="must match"):
        cloud_bridge.upload_app_media(
            "app_other123",
            str(media),
            _cwd=tmp_path,
            _client_factory=lambda: object(),
        )
    with pytest.raises(cloud_bridge.BridgeRequestError, match="inside"):
        cloud_bridge.upload_app_media(
            "app_cloud123",
            str(outside),
            _cwd=tmp_path,
            _client_factory=lambda: object(),
        )
    with pytest.raises(cloud_bridge.BridgeRequestError, match="safe relative"):
        cloud_bridge.upload_app_media(
            "app_cloud123",
            str(media),
            destination="../private.png",
            _cwd=tmp_path,
            _client_factory=lambda: object(),
        )


def test_media_read_url_and_delete_are_structured() -> None:
    calls = []

    class FakeCloud:
        def list_app_media(self, app_id):
            calls.append(("list", app_id))
            return {"media": []}

        def get_app_media_url(self, app_id, media_id, *, expires_in):
            calls.append(("url", app_id, media_id, expires_in))
            return {"url": "https://media.example/signed"}

        def delete_app_media(self, app_id, media_id):
            calls.append(("delete", app_id, media_id))
            return {"deleted": True}

    cloud = FakeCloud()

    def factory():
        return cloud

    assert (
        cloud_bridge.list_app_media("app_cloud123", _client_factory=factory)["media"] == []
    )
    assert cloud_bridge.get_app_media_url(
        "app_cloud123",
        "med_cloud123",
        expires_in=600,
        _client_factory=factory,
    )["url"].startswith("https://")
    assert (
        cloud_bridge.delete_app_media(
            "app_cloud123", "med_cloud123", _client_factory=factory
        )["deleted"]
        is True
    )
    assert calls == [
        ("list", "app_cloud123"),
        ("url", "app_cloud123", "med_cloud123", 600),
        ("delete", "app_cloud123", "med_cloud123"),
    ]


def test_database_tools_are_structured_and_never_accept_sql() -> None:
    calls = []

    class FakeCloud:
        def list_database_tables(self, app_id):
            calls.append(("list", app_id))
            return {"tables": ["customers"]}

        def inspect_database_table(self, app_id, table):
            calls.append(("inspect", app_id, table))
            return {"columns": ["id", "status"]}

        def query_database_table(
            self, app_id, table, *, columns, filters, order, limit
        ):
            calls.append(("query", app_id, table, columns, filters, order, limit))
            return {"rows": [{"id": 1}]}

        def list_database_migrations(self, app_id):
            calls.append(("migrations", app_id))
            return {"migrations": []}

    cloud = FakeCloud()

    def factory():
        return cloud

    assert cloud_bridge.list_database_tables("app_cloud123", _client_factory=factory)[
        "tables"
    ] == ["customers"]
    assert cloud_bridge.inspect_database_table(
        "app_cloud123", "public.customers", _client_factory=factory
    )["columns"] == ["id", "status"]
    result = cloud_bridge.query_database_table(
        "app_cloud123",
        "customers",
        columns=["id"],
        filters=[{"column": "status", "op": "eq", "value": "active"}],
        order=[{"column": "id", "direction": "desc"}],
        limit=10,
        _client_factory=factory,
    )
    cloud_bridge.list_database_migrations("app_cloud123", _client_factory=factory)

    assert result["rows"] == [{"id": 1}]
    assert calls[-2] == (
        "query",
        "app_cloud123",
        "customers",
        ["id"],
        [{"column": "status", "op": "eq", "value": "active"}],
        [{"column": "id", "direction": "desc"}],
        10,
    )
    assert calls[-1] == ("migrations", "app_cloud123")
    with pytest.raises(cloud_bridge.BridgeRequestError):
        cloud_bridge.query_database_table(
            "app_cloud123",
            "customers/../../secrets",
            _client_factory=factory,
        )


@pytest.mark.parametrize(
    "argv",
    [
        ["deploy", "--source-dir", "/etc", "--operation-id", "pi-op"],
        ["deploy"],
        [
            "query-database-table",
            "--app-id",
            "app_cloud123",
            "--table",
            "customers",
            "--sql",
            "delete from customers",
        ],
    ],
)
def test_request_protocol_rejects_source_paths_missing_operation_ids_and_sql(
    argv, capsys
) -> None:
    rc = cloud_bridge.main(argv)
    payload = json.loads(capsys.readouterr().out)

    assert rc == 2
    assert payload["status"] == "error"
    assert payload["error_code"] == "invalid_request"


def test_query_protocol_parses_structured_json() -> None:
    action, request = cloud_bridge._parse_request(
        [
            "query-database-table",
            "--app-id",
            "app_cloud123",
            "--table",
            "customers",
            "--columns-json",
            '["id"]',
            "--filters-json",
            '[{"column":"status","op":"eq","value":"active"}]',
            "--order-json",
            '[{"column":"id","direction":"asc"}]',
            "--limit",
            "25",
        ]
    )

    assert action == "query-database-table"
    assert request["columns"] == ["id"]
    assert request["filters"][0]["value"] == "active"
    assert request["order"] == [{"column": "id", "direction": "asc"}]
    assert request["limit"] == 25


def test_expected_cloud_error_is_structured_transport_success(
    tmp_path, capsys, monkeypatch
) -> None:
    _write_manifest(tmp_path)
    monkeypatch.chdir(tmp_path)

    class BrokenCloud:
        def deploy_pipeline(
            self, app_dir, *, components, idempotency_key
        ) -> dict[str, object]:
            raise cloud_bridge.CloudDeployError("cloud unavailable")

    monkeypatch.setattr(cloud_bridge, "CloudDeployClient", BrokenCloud)
    rc = cloud_bridge.main(["deploy", "--operation-id", "pi-operation123"])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["status"] == "error"
    assert payload["error_code"] == "cloud_error"
    assert payload["error"] == "cloud unavailable"


def test_unexpected_bridge_failure_is_json_without_traceback(
    tmp_path, capsys, monkeypatch
) -> None:
    _write_manifest(tmp_path)
    monkeypatch.chdir(tmp_path)

    class BrokenCloud:
        def deploy_pipeline(
            self, app_dir, *, components, idempotency_key
        ) -> dict[str, object]:
            raise RuntimeError("host broke")

    monkeypatch.setattr(cloud_bridge, "CloudDeployClient", BrokenCloud)
    rc = cloud_bridge.main(["deploy", "--operation-id", "pi-operation123"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert rc == 1
    assert captured.err == ""
    assert payload == {
        "status": "error",
        "error_code": "bridge_failure",
        "error": "cloud bridge failed: host broke",
        "bridge_version": 3,
    }


def test_bridge_runs_as_absolute_script_from_project_cwd(tmp_path) -> None:
    bridge = Path(cloud_bridge.__file__).resolve()
    result = subprocess.run(
        [sys.executable, str(bridge), "status", "--pipeline-id", "../escape"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 2
    assert result.stderr == ""
    assert json.loads(result.stdout)["error_code"] == "invalid_request"


def test_extension_uses_tool_call_id_for_idempotency() -> None:
    extension = Path(cloud_bridge.__file__).resolve().parent / "extensions" / "deploy.ts"
    source = extension.read_text(encoding="utf-8")
    assert "stableOperationId(toolCallId)" in source
    assert '"--operation-id"' in source
    assert "local Docker deployment" in source


def test_trusted_extension_loads_cloud_tools_in_pi_rpc(tmp_path) -> None:
    pi = shutil.which("pi")
    if pi is None:
        pytest.skip("Pi is not installed")

    extension = Path(cloud_bridge.__file__).resolve().parent / "extensions" / "deploy.ts"
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
            ",".join(CLOUD_TOOL_NAMES),
            "-e",
            str(extension),
        ],
        input='{"id":"state-1","type":"get_state"}\n',
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    responses = [
        json.loads(line) for line in result.stdout.splitlines() if line.strip()
    ]
    assert result.returncode == 0, result.stderr
    assert any(
        item.get("type") == "response"
        and item.get("id") == "state-1"
        and item.get("success") is True
        for item in responses
    ), (result.stdout, result.stderr)
