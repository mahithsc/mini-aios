"""MCP server exposing cloud deployment tools to a Codex session.

The three production tools package the current directory into a deterministic
artifact and send it to aios-cloud. Provider credentials and user secret values
never enter this process. The original local ``deploy`` compatibility tool is
disabled unless ``AIOS_ENABLE_LEGACY_LOCAL_DEPLOY=1`` is explicitly set.

Run standalone (how Codex launches it):
    python -m aios_core.deploy.mcp_server
"""

from __future__ import annotations

import os
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from .cloud_client import CloudDeployClient, CloudDeployError
from .deployer import deploy as _deploy
from .manifest import ManifestValidationError, find_deployment_root
from .store import ProjectStore

mcp = FastMCP("aios-deploy")


@mcp.tool()
def deploy(slug: str) -> dict:
    """Build and run the project in the current directory as a live service.

    Requires a project.json in the working directory:
    {"run": ["python","app.py"], "port": 8000, "image": "python:3.12-slim"}.
    Returns {status:"running", url, ...} on success, or {status:"error", error,
    logs} on failure — read `logs` to fix the app and call deploy again.
    """
    if os.getenv("AIOS_ENABLE_LEGACY_LOCAL_DEPLOY", "").lower() not in {
        "1",
        "true",
        "yes",
    }:
        return {
            "status": "error",
            "error": (
                "Legacy device-local deployment is disabled; use deploy_database, "
                "deploy_server, or deploy_frontend"
            ),
        }
    return _deploy(slug, os.getcwd(), store=ProjectStore())


def _deploy_cloud(component: Literal["database", "server", "frontend"]) -> dict:
    try:
        current_dir = os.getcwd()
        app_dir = find_deployment_root(current_dir) or current_dir
        result = CloudDeployClient().deploy(component, app_dir)
        deployment_id = result.get("id")
        if isinstance(deployment_id, str) and result.get("status") not in {
            "active",
            "failed",
            "cancelled",
            "rolled_back",
            "superseded",
        }:
            result["next_action"] = {
                "tool": "get_deployment_status",
                "deployment_id": deployment_id,
            }
        return result
    except (CloudDeployError, ManifestValidationError, OSError) as exc:
        return {"status": "error", "component": component, "error": str(exc)}


@mcp.tool()
def deploy_database() -> dict:
    """Upload this app artifact and deploy its database through aios-cloud/Supabase."""
    return _deploy_cloud("database")


@mcp.tool()
def deploy_server() -> dict:
    """Upload this app artifact and deploy its backend through aios-cloud/DigitalOcean."""
    return _deploy_cloud("server")


@mcp.tool()
def deploy_frontend() -> dict:
    """Upload this app artifact and deploy its frontend through aios-cloud/Vercel."""
    return _deploy_cloud("frontend")


@mcp.tool()
def get_deployment_status(deployment_id: str) -> dict:
    """Get durable cloud deployment state, errors, and the live URL when ready."""
    return _cloud_read(lambda client: client.get_deployment(deployment_id))


@mcp.tool()
def get_deployment_events(deployment_id: str, after: int = -1) -> dict:
    """Read deployment progress and action-required events after a cursor."""
    return _cloud_read(
        lambda client: client.get_deployment_events(deployment_id, after=after)
    )


@mcp.tool()
def get_app_info(app_id: str) -> dict:
    """Get app metadata plus active URLs and latest state for every component."""
    return _cloud_read(lambda client: client.get_app_info(app_id))


@mcp.tool()
def check_app_status(app_id: str) -> dict:
    """Check every deploy pipeline and its artifact upload/verification state.

    Returns normalized phases such as queued, in_process, action_required,
    completed, or failed; the raw latest deployment and event; active URLs; and
    whether each artifact was uploaded to and verified in private cloud storage.
    """
    return _cloud_read(lambda client: client.check_app_status(app_id))


@mcp.tool()
def cancel_cloud_deployment(deployment_id: str) -> dict:
    """Cancel a queued or running cloud deployment."""
    return _cloud_read(lambda client: client.cancel_deployment(deployment_id))


@mcp.tool()
def resume_cloud_deployment(deployment_id: str) -> dict:
    """Resume a deployment after its requested secrets or confirmation exist."""
    return _cloud_read(lambda client: client.resume_deployment(deployment_id))


@mcp.tool()
def rollback_cloud_deployment(deployment_id: str) -> dict:
    """Redeploy a prior frontend/server artifact as a new immutable release."""
    return _cloud_read(lambda client: client.rollback_deployment(deployment_id))


@mcp.tool()
def delete_cloud_app(app_id: str) -> dict:
    """Queue permanent cleanup of an app and all of its provider resources."""
    return _cloud_read(lambda client: client.delete_app(app_id))


@mcp.tool()
def upload_app_media(
    app_id: str,
    local_path: str,
    destination: str | None = None,
    content_type: str | None = None,
) -> dict:
    """Upload workspace media through aios-cloud into the app's private storage.

    The file must be inside the current app workspace. Provider credentials are
    never exposed; aios-cloud issues a short-lived object-scoped upload URL.
    """
    try:
        current_dir = os.getcwd()
        app_root = find_deployment_root(current_dir) or current_dir
        return CloudDeployClient().upload_app_media(
            app_id,
            local_path,
            destination=destination,
            content_type=content_type,
            allowed_root=app_root,
        )
    except (CloudDeployError, OSError) as exc:
        return {"status": "error", "error": str(exc)}


@mcp.tool()
def list_app_media(app_id: str) -> dict:
    """List verified media objects stored for an app."""
    return _cloud_read(lambda client: client.list_app_media(app_id))


@mcp.tool()
def get_app_media_url(
    app_id: str, media_id: str, expires_in: int = 3600
) -> dict:
    """Create a temporary private download URL for an app media object."""
    return _cloud_read(
        lambda client: client.get_app_media_url(
            app_id, media_id, expires_in=expires_in
        )
    )


@mcp.tool()
def delete_app_media(app_id: str, media_id: str) -> dict:
    """Delete an app media object through aios-cloud."""
    return _cloud_read(lambda client: client.delete_app_media(app_id, media_id))


def _cloud_read(operation) -> dict:
    try:
        return operation(CloudDeployClient())
    except CloudDeployError as exc:
        return {"status": "error", "error": str(exc)}


@mcp.tool()
def list_database_tables(app_id: str) -> dict:
    """List app tables and whether Codex row reads are allowed for each table."""
    return _cloud_read(lambda client: client.list_database_tables(app_id))


@mcp.tool()
def inspect_database_table(app_id: str, table: str) -> dict:
    """Inspect columns, constraints, indexes, row estimate, and migration history."""
    return _cloud_read(lambda client: client.inspect_database_table(app_id, table))


@mcp.tool()
def query_database_table(
    app_id: str,
    table: str,
    columns: list[str] | None = None,
    filters: list[dict[str, Any]] | None = None,
    order: list[dict[str, Any]] | None = None,
    limit: int = 100,
) -> dict:
    """Run a policy-checked structured, read-only query through aios-cloud."""
    return _cloud_read(
        lambda client: client.query_database_table(
            app_id,
            table,
            columns=columns,
            filters=filters,
            order=order,
            limit=limit,
        )
    )


@mcp.tool()
def list_database_migrations(app_id: str) -> dict:
    """List immutable checksummed migrations applied to an app database."""
    return _cloud_read(lambda client: client.list_database_migrations(app_id))


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
