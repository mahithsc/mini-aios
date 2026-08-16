from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import httpx

from aios_core.deploy.cloud_client import CloudDeployClient
from aios_core.deploy import mcp_server


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
                    "components": {
                        "server": {"url": "https://server.example.test"}
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
    assert client.list_secret_metadata()["secrets"][0]["id"] == "sec_cloud123"
    assert requests == [
        ("POST", "/v1/apps"),
        ("GET", "/v1/apps"),
        ("GET", "/v1/apps/app_cloud123/info"),
        ("GET", "/v1/device/secrets"),
    ]


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


def test_legacy_local_deploy_is_disabled_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AIOS_ENABLE_LEGACY_LOCAL_DEPLOY", raising=False)
    monkeypatch.setattr(
        mcp_server,
        "_deploy",
        lambda *args, **kwargs: pytest.fail("legacy deploy should not execute"),
    )

    result = mcp_server.deploy("legacy")

    assert result["status"] == "error"
    assert "disabled" in result["error"]


def test_mcp_get_app_info_returns_cloud_endpoint_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeCloudClient:
        def get_app_info(self, app_id: str) -> dict:
            return {
                "app": {"id": app_id},
                "components": {
                    "server": {"url": "https://server.example.test"},
                    "frontend": {"url": "https://frontend.example.test"},
                },
            }

    monkeypatch.setattr(mcp_server, "CloudDeployClient", FakeCloudClient)
    result = mcp_server.get_app_info("app_cloud123")
    assert result["app"]["id"] == "app_cloud123"
    assert result["components"]["server"]["url"] == (
        "https://server.example.test"
    )
    assert result["components"]["frontend"]["url"] == (
        "https://frontend.example.test"
    )
