from __future__ import annotations

import io
import json
import sqlite3
import tarfile
from pathlib import Path

import httpx
import pytest

from aios_core.deploy import cloud_client
from aios_core.deploy.cloud_client import CloudDeployClient, CloudDeployError
from aios_core.release import DATABASE_SCHEMA_VERSION


def test_paired_device_token_uses_initialized_device_link(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from aios_core import db

    db_path = str(tmp_path / "aios.db")
    db.initialize_app_db(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO device_link (id, device_token) VALUES (1, ?)",
            ("paired-token",),
        )
        migration = connection.execute(
            "SELECT name, checksum FROM schema_migrations WHERE version = 5"
        ).fetchone()
        max_schema_version = connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0]
    monkeypatch.setattr(db, "get_db_connection", lambda: sqlite3.connect(db_path))

    assert cloud_client.paired_device_token() == "paired-token"
    assert migration == ("cloud_deployment_runtime", "cloud-deploy-v1")
    assert max_schema_version == DATABASE_SCHEMA_VERSION


@pytest.mark.parametrize("corruption", ["checksum", "future"])
def test_database_initialization_rejects_incompatible_migration_history(
    tmp_path: Path, corruption: str
) -> None:
    from aios_core import db

    db_path = str(tmp_path / "aios.db")
    db.initialize_app_db(db_path)
    with sqlite3.connect(db_path) as connection:
        if corruption == "checksum":
            connection.execute(
                "UPDATE schema_migrations SET checksum = 'wrong' WHERE version = 5"
            )
        else:
            connection.execute(
                """
                INSERT INTO schema_migrations (
                    version, name, checksum, applied_at, app_release
                ) VALUES (6, 'future', 'future-v1', 1, 'future-release')
                """
            )

    with pytest.raises(RuntimeError, match="newer|name/checksum"):
        db.initialize_app_db(db_path)


def test_cloud_client_uses_production_url_and_paired_device_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AIOS_CLOUD_URL", raising=False)
    monkeypatch.delenv("AIOS_CLOUD_DEVICE_TOKEN", raising=False)
    monkeypatch.setattr(cloud_client, "_paired_device_token", lambda: "paired-token")

    client = CloudDeployClient()

    assert client._base_url == "https://computer.winkapiserver.org"
    assert client._device_token == "paired-token"


def test_cloud_client_explains_when_device_is_not_paired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AIOS_CLOUD_URL", raising=False)
    monkeypatch.delenv("AIOS_CLOUD_DEVICE_TOKEN", raising=False)
    monkeypatch.setattr(cloud_client, "_paired_device_token", lambda: "")

    with pytest.raises(CloudDeployError, match="not paired"):
        CloudDeployClient()


def test_cloud_client_enqueues_tiered_pipeline() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/apps/app_cloud123/deployment-pipelines"
        assert request.headers["authorization"] == "Bearer device-token"
        assert request.headers["idempotency-key"] == "pipeline-key"
        assert json.loads(request.content) == {
            "artifact_id": "art_cloud123",
            "components": ["database", "server", "frontend"],
        }
        return httpx.Response(
            201,
            json={
                "id": "pip_cloud123",
                "app_id": "app_cloud123",
                "artifact_id": "art_cloud123",
                "requested_components": ["database", "server", "frontend"],
                "status": "running",
                "deployments": [],
                "created_at": 1,
                "updated_at": 1,
                "completed_at": None,
            },
        )

    transport = httpx.MockTransport(handler)
    client = CloudDeployClient(
        base_url="https://cloud.example",
        device_token="device-token",
        client_factory=lambda: httpx.Client(transport=transport),
    )
    result = client.enqueue_pipeline(
        app_id="app_cloud123",
        artifact_id="art_cloud123",
        components=["database", "server", "frontend"],
        idempotency_key="pipeline-key",
    )
    assert result["id"] == "pip_cloud123"


def test_cloud_client_normalizes_pipeline_components() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["components"] == [
            "database",
            "server",
            "frontend",
        ]
        return httpx.Response(201, json={"id": "pip_cloud123", "status": "running"})

    client = CloudDeployClient(
        base_url="https://cloud.example",
        device_token="device-token",
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)),
    )

    client.enqueue_pipeline(
        app_id="app_cloud123",
        artifact_id="art_cloud123",
        components=["frontend", "database", "server", "database"],
        idempotency_key="pipeline-key",
    )

    with pytest.raises(CloudDeployError, match="at least one"):
        client.enqueue_pipeline(
            app_id="app_cloud123",
            artifact_id="art_cloud123",
            components=[],
            idempotency_key="pipeline-key",
        )

    with pytest.raises(CloudDeployError, match="stable idempotency_key"):
        client.enqueue_pipeline(
            app_id="app_cloud123",
            artifact_id="art_cloud123",
            components=["server"],
        )


def _write_app(root: Path) -> None:
    (root / "server").mkdir()
    (root / "server" / "Dockerfile").write_text("FROM python:3.12-slim\n")
    (root / "server" / "app.py").write_text("print('ready')\n")
    (root / "aios.deploy.yaml").write_text(
        """
version: 1
app_id: app_cloud123
server:
  source: server
  dockerfile: server/Dockerfile
""".lstrip()
    )


def test_cloud_client_rejects_undeclared_pipeline_component(tmp_path: Path) -> None:
    _write_app(tmp_path)
    client = CloudDeployClient(
        base_url="https://cloud.example",
        device_token="device-token",
    )

    with pytest.raises(CloudDeployError, match="not declared"):
        client.deploy_pipeline(
            tmp_path,
            components=["frontend"],
            idempotency_key="pipeline-key",
        )

    with pytest.raises(CloudDeployError, match="at least one"):
        client.deploy_pipeline(
            tmp_path,
            components=[],
            idempotency_key="pipeline-key",
        )


def test_cloud_client_creates_and_lists_apps() -> None:
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        assert request.headers["authorization"] == "Bearer device-token"
        if request.method == "POST":
            assert json.loads(request.content) == {"name": "Example App"}
            return httpx.Response(
                201, json={"id": "app_cloud123", "name": "Example App"}
            )
        if request.url.path == "/v1/apps":
            return httpx.Response(200, json=[{"id": "app_cloud123"}])
        if request.url.path == "/v1/apps/app_cloud123/info":
            return httpx.Response(
                200,
                json={
                    "app": {"id": "app_cloud123"},
                    "components": {"server": {"url": "https://server.example.test"}},
                },
            )
        if request.url.path == "/v1/apps/app_cloud123/status":
            return httpx.Response(
                200,
                json={
                    "app": {"id": "app_cloud123"},
                    "overall_status": "in_process",
                    "components": {
                        "server": {
                            "phase": "in_process",
                            "artifact_uploaded": True,
                            "artifact_verified": True,
                        }
                    },
                },
            )
        return httpx.Response(
            200,
            json=[
                {
                    "id": "sec_cloud123",
                    "kind": "api_key",
                    "label": "Vendor",
                    "configured": True,
                }
            ],
        )

    transport = httpx.MockTransport(handler)
    client = CloudDeployClient(
        base_url="https://cloud.example",
        device_token="device-token",
        client_factory=lambda: httpx.Client(transport=transport),
    )

    assert client.create_app(" Example App ")["id"] == "app_cloud123"
    assert client.list_apps()["apps"] == [{"id": "app_cloud123"}]
    assert client.get_app_info("app_cloud123")["components"]["server"]["url"] == (
        "https://server.example.test"
    )
    status = client.check_app_status("app_cloud123")
    assert status["overall_status"] == "in_process"
    assert status["components"]["server"]["artifact_verified"] is True
    assert client.list_secret_metadata()["secrets"][0]["id"] == "sec_cloud123"
    assert requests == [
        ("POST", "/v1/apps"),
        ("GET", "/v1/apps"),
        ("GET", "/v1/apps/app_cloud123/info"),
        ("GET", "/v1/apps/app_cloud123/status"),
        ("GET", "/v1/device/secrets"),
    ]


def test_cloud_client_uploads_app_media_with_scoped_signed_url(tmp_path: Path) -> None:
    media_path = tmp_path / "hero.png"
    media_path.write_bytes(b"png-bytes")
    requests: list[tuple[str, str, bool]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(
            (
                request.method,
                request.url.path,
                "authorization" in request.headers,
            )
        )
        if request.url.path == "/v1/apps/app_cloud123/media/uploads":
            payload = json.loads(request.content)
            assert payload["filename"] == "hero.png"
            assert payload["content_type"] == "image/png"
            assert payload["destination"] == "marketing/hero.png"
            return httpx.Response(
                201,
                json={
                    "media": {"id": "med_cloud123", "status": "pending_upload"},
                    "upload_url": "https://uploads.example/media-token",
                    "expires_in": 7200,
                },
            )
        if request.url.host == "uploads.example":
            assert request.headers["content-type"] == "image/png"
            return httpx.Response(200)
        if request.url.path.endswith("/med_cloud123/complete"):
            return httpx.Response(
                200,
                json={
                    "media": {
                        "id": "med_cloud123",
                        "app_id": "app_cloud123",
                        "object_key": "app_cloud123/marketing/hero.png",
                        "status": "ready",
                    }
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    client = CloudDeployClient(
        base_url="https://cloud.example",
        device_token="device-token",
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = client.upload_app_media(
        "app_cloud123",
        media_path,
        destination="marketing/hero.png",
        allowed_root=tmp_path,
    )

    assert result["status"] == "ready"
    assert requests == [
        ("POST", "/v1/apps/app_cloud123/media/uploads", True),
        ("PUT", "/media-token", False),
        ("POST", "/v1/apps/app_cloud123/media/med_cloud123/complete", True),
    ]


def test_cloud_client_rejects_media_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "private.png"
    outside.write_bytes(b"not allowed")
    client = CloudDeployClient(
        base_url="https://cloud.example",
        device_token="device-token",
    )

    with pytest.raises(CloudDeployError, match="inside the current app workspace"):
        client.upload_app_media("app_cloud123", outside, allowed_root=workspace)


def test_cloud_client_uploads_full_archive_and_enqueues_component(
    tmp_path: Path,
) -> None:
    _write_app(tmp_path)
    uploaded = bytearray()
    registered: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal registered
        if request.url.path == "/v1/artifacts/uploads":
            assert request.headers["authorization"] == "Bearer device-token"
            registered = json.loads(request.content)
            return httpx.Response(
                201,
                json={
                    "artifact": {
                        "id": "art_cloud123",
                        "app_id": "app_cloud123",
                        "sha256": registered["sha256"],
                        "size": registered["size"],
                        "manifest": registered["manifest"],
                        "status": "pending_upload",
                        "created_at": 1,
                    },
                    "upload_required": True,
                    "upload_url": "https://upload.example/signed",
                    "expires_in": 7200,
                },
            )
        if request.url.host == "upload.example":
            uploaded.extend(request.content)
            assert request.headers["content-type"] == "application/gzip"
            return httpx.Response(200, json={"Key": "artifact"})
        if request.url.path == "/v1/artifacts/art_cloud123/complete":
            return httpx.Response(
                200,
                json={
                    "artifact": {
                        "id": "art_cloud123",
                        "app_id": "app_cloud123",
                        "sha256": registered["sha256"],
                        "size": registered["size"],
                        "manifest": registered["manifest"],
                        "status": "ready",
                        "created_at": 1,
                    }
                },
            )
        if request.url.path == "/v1/apps/app_cloud123/deployments/server":
            assert request.headers["idempotency-key"].startswith("device-")
            assert json.loads(request.content) == {"artifact_id": "art_cloud123"}
            return httpx.Response(
                201,
                json={
                    "id": "dep_cloud123",
                    "app_id": "app_cloud123",
                    "artifact_id": "art_cloud123",
                    "component": "server",
                    "status": "queued",
                    "active": False,
                    "url": None,
                    "error_code": None,
                    "created_at": 1,
                    "updated_at": 1,
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    transport = httpx.MockTransport(handler)
    client = CloudDeployClient(
        base_url="https://cloud.example",
        device_token="device-token",
        client_factory=lambda: httpx.Client(transport=transport),
    )
    result = client.deploy("server", tmp_path)

    assert result["id"] == "dep_cloud123"
    assert result["status"] == "queued"
    assert len(uploaded) == registered["size"]
    with tarfile.open(fileobj=io.BytesIO(uploaded), mode="r:gz") as archive:
        dockerfile = archive.extractfile("server/Dockerfile")
        assert dockerfile is not None
        assert dockerfile.read() == b"FROM python:3.12-slim\n"


def test_cloud_client_database_gateway_is_structured_and_authenticated() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["authorization"] == "Bearer device-token"
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "app_id": "app_cloud123",
                    "table": "customers",
                    "columns": ["id"],
                    "rows": [{"id": 1}],
                    "row_count": 1,
                    "truncated": False,
                },
            )
        return httpx.Response(200, json={"app_id": "app_cloud123", "tables": []})

    transport = httpx.MockTransport(handler)
    client = CloudDeployClient(
        base_url="https://cloud.example",
        device_token="device-token",
        client_factory=lambda: httpx.Client(transport=transport),
    )
    assert client.list_database_tables("app_cloud123")["tables"] == []
    result = client.query_database_table(
        "app_cloud123",
        "customers",
        columns=["id"],
        filters=[{"column": "status", "op": "eq", "value": "active"}],
        limit=10,
    )
    assert result["rows"] == [{"id": 1}]
    assert json.loads(requests[-1].content) == {
        "columns": ["id"],
        "filters": [{"column": "status", "op": "eq", "value": "active"}],
        "order": [],
        "limit": 10,
    }


def test_cloud_client_deployment_lifecycle_requests() -> None:
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        assert request.headers["authorization"] == "Bearer device-token"
        if request.url.path.endswith("/events"):
            assert request.url.params["after"] == "4"
            return httpx.Response(200, json={"events": [], "cursor": 4})
        return httpx.Response(
            200,
            json={
                "id": "dep_cloud123",
                "status": "building",
                "active": False,
            },
        )

    transport = httpx.MockTransport(handler)
    client = CloudDeployClient(
        base_url="https://cloud.example",
        device_token="device-token",
        client_factory=lambda: httpx.Client(transport=transport),
    )
    assert client.get_deployment("dep_cloud123")["status"] == "building"
    assert client.get_deployment_events("dep_cloud123", after=4)["cursor"] == 4
    client.cancel_deployment("dep_cloud123")
    client.resume_deployment("dep_cloud123")
    client.rollback_deployment("dep_cloud123")
    client.delete_app("app_cloud123")
    assert requests == [
        ("GET", "/v1/deployments/dep_cloud123"),
        ("GET", "/v1/deployments/dep_cloud123/events"),
        ("POST", "/v1/deployments/dep_cloud123/cancel"),
        ("POST", "/v1/deployments/dep_cloud123/resume"),
        ("POST", "/v1/deployments/dep_cloud123/rollback"),
        ("DELETE", "/v1/apps/app_cloud123"),
    ]
