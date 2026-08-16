"""Thin device-side client for the aios-cloud deployment control plane."""

from __future__ import annotations

import os
import tempfile
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Literal

import httpx

from .archive import ArtifactArchive, create_artifact_archive

DeploymentComponent = Literal["database", "server", "frontend"]


class CloudDeployError(RuntimeError):
    """A cloud deployment request could not be completed."""


class CloudDeployClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        device_token: str | None = None,
        client_factory: Callable[[], httpx.Client] | None = None,
    ) -> None:
        self._base_url = (base_url or os.getenv("AIOS_CLOUD_URL", "")).rstrip("/")
        self._device_token = device_token or os.getenv("AIOS_CLOUD_DEVICE_TOKEN", "")
        self._client_factory = client_factory or (
            lambda: httpx.Client(timeout=httpx.Timeout(60.0, read=300.0))
        )
        if not self._base_url:
            raise CloudDeployError("AIOS_CLOUD_URL is not configured")
        if not self._device_token:
            raise CloudDeployError("AIOS_CLOUD_DEVICE_TOKEN is not configured")

    def create_app(self, name: str) -> dict[str, Any]:
        """Reserve an app identity before Codex creates its deploy manifest."""
        normalized = name.strip()
        if not normalized:
            raise CloudDeployError("App name cannot be empty")
        return self._request_json("POST", "/v1/apps", json={"name": normalized})

    def list_apps(self) -> dict[str, Any]:
        """List cloud app identities owned by the authenticated user."""
        return {"apps": self._request_list("GET", "/v1/apps")}

    def get_app_info(self, app_id: str) -> dict[str, Any]:
        """Get durable app metadata and current component endpoints."""
        return self._request_json("GET", f"/v1/apps/{app_id}/info")

    def delete_app(self, app_id: str) -> dict[str, Any]:
        return self._request_json("DELETE", f"/v1/apps/{app_id}")

    def list_secret_metadata(self) -> dict[str, Any]:
        """List cloud secret references without fetching secret values."""
        return {"secrets": self._request_list("GET", "/v1/device/secrets")}

    def deploy(
        self,
        component: DeploymentComponent,
        app_dir: str | Path,
    ) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="aios-artifact-") as directory:
            archive = create_artifact_archive(
                app_dir,
                Path(directory) / "artifact.tar.gz",
            )
            artifact_id = self.upload_artifact(archive)
            return self.enqueue_deployment(
                component=component,
                app_id=archive.manifest.app_id,
                artifact_id=artifact_id,
            )

    def upload_artifact(self, archive: ArtifactArchive) -> str:
        manifest = archive.manifest.model_dump(mode="json", exclude_none=True)
        registration = self._request_json(
            "POST",
            "/v1/artifacts/uploads",
            json={
                "app_id": archive.manifest.app_id,
                "sha256": archive.sha256,
                "size": archive.size,
                "manifest": manifest,
            },
        )
        artifact = registration.get("artifact")
        if not isinstance(artifact, dict) or not isinstance(artifact.get("id"), str):
            raise CloudDeployError("Cloud returned an invalid artifact registration")
        artifact_id = artifact["id"]
        if registration.get("upload_required"):
            upload_url = registration.get("upload_url")
            if not isinstance(upload_url, str) or not upload_url:
                raise CloudDeployError("Cloud did not return an artifact upload URL")
            with self._client_factory() as client:
                response = client.put(
                    upload_url,
                    headers={
                        "Content-Type": "application/gzip",
                        "Cache-Control": "max-age=31536000, immutable",
                        "x-upsert": "false",
                        "Content-Length": str(archive.size),
                    },
                    content=_file_chunks(archive.path),
                )
            self._raise_for_status(response, "Artifact upload failed")
            completed = self._request_json(
                "POST",
                f"/v1/artifacts/{artifact_id}/complete",
            )
            completed_artifact = completed.get("artifact")
            if (
                not isinstance(completed_artifact, dict)
                or completed_artifact.get("status") != "ready"
            ):
                raise CloudDeployError("Cloud did not mark the artifact ready")
        elif artifact.get("status") != "ready":
            raise CloudDeployError("Artifact is not ready and cannot be uploaded")
        return artifact_id

    def enqueue_deployment(
        self,
        *,
        component: DeploymentComponent,
        app_id: str,
        artifact_id: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        key = idempotency_key or f"device-{uuid.uuid4().hex}"
        return self._request_json(
            "POST",
            f"/v1/apps/{app_id}/deployments/{component}",
            headers={"Idempotency-Key": key},
            json={"artifact_id": artifact_id},
        )

    def get_deployment(self, deployment_id: str) -> dict[str, Any]:
        return self._request_json("GET", f"/v1/deployments/{deployment_id}")

    def get_deployment_events(
        self,
        deployment_id: str,
        *,
        after: int = -1,
    ) -> dict[str, Any]:
        return self._request_json(
            "GET",
            f"/v1/deployments/{deployment_id}/events",
            params={"after": after},
        )

    def cancel_deployment(self, deployment_id: str) -> dict[str, Any]:
        return self._request_json(
            "POST",
            f"/v1/deployments/{deployment_id}/cancel",
        )

    def resume_deployment(self, deployment_id: str) -> dict[str, Any]:
        return self._request_json(
            "POST",
            f"/v1/deployments/{deployment_id}/resume",
        )

    def rollback_deployment(self, deployment_id: str) -> dict[str, Any]:
        return self._request_json(
            "POST",
            f"/v1/deployments/{deployment_id}/rollback",
        )

    def list_database_tables(self, app_id: str) -> dict[str, Any]:
        return self._request_json("GET", f"/v1/apps/{app_id}/database/tables")

    def inspect_database_table(self, app_id: str, table: str) -> dict[str, Any]:
        return self._request_json(
            "GET",
            f"/v1/apps/{app_id}/database/tables/{table}",
        )

    def query_database_table(
        self,
        app_id: str,
        table: str,
        *,
        columns: list[str] | None = None,
        filters: list[dict[str, Any]] | None = None,
        order: list[dict[str, Any]] | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        return self._request_json(
            "POST",
            f"/v1/apps/{app_id}/database/tables/{table}/query",
            json={
                "columns": columns or [],
                "filters": filters or [],
                "order": order or [],
                "limit": limit,
            },
        )

    def list_database_migrations(self, app_id: str) -> dict[str, Any]:
        return self._request_json(
            "GET",
            f"/v1/apps/{app_id}/database/migrations",
        )

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        request_headers = {
            "Authorization": f"Bearer {self._device_token}",
            **(headers or {}),
        }
        try:
            with self._client_factory() as client:
                response = client.request(
                    method,
                    f"{self._base_url}{path}",
                    headers=request_headers,
                    **kwargs,
                )
        except httpx.HTTPError as exc:
            raise CloudDeployError(f"Could not reach aios-cloud: {exc}") from exc
        self._raise_for_status(response, "aios-cloud request failed")
        try:
            payload = response.json()
        except ValueError as exc:
            raise CloudDeployError("aios-cloud returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise CloudDeployError("aios-cloud returned an invalid response")
        return payload

    def _request_list(
        self,
        method: str,
        path: str,
    ) -> list[dict[str, Any]]:
        request_headers = {"Authorization": f"Bearer {self._device_token}"}
        try:
            with self._client_factory() as client:
                response = client.request(
                    method,
                    f"{self._base_url}{path}",
                    headers=request_headers,
                )
        except httpx.HTTPError as exc:
            raise CloudDeployError(f"Could not reach aios-cloud: {exc}") from exc
        self._raise_for_status(response, "aios-cloud request failed")
        try:
            payload = response.json()
        except ValueError as exc:
            raise CloudDeployError("aios-cloud returned invalid JSON") from exc
        if not isinstance(payload, list) or any(
            not isinstance(item, dict) for item in payload
        ):
            raise CloudDeployError("aios-cloud returned an invalid response")
        return payload

    @staticmethod
    def _raise_for_status(response: httpx.Response, message: str) -> None:
        if response.is_success:
            return
        detail = ""
        try:
            payload = response.json()
            if isinstance(payload, dict) and payload.get("detail"):
                detail = f": {payload['detail']}"
        except ValueError:
            pass
        raise CloudDeployError(f"{message} (HTTP {response.status_code}){detail}")


def _file_chunks(path: Path) -> Iterator[bytes]:
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            yield chunk
