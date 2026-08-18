"""Main-agent tools for durable app workspaces and cloud app identity.

The cloud/workspace tools route Pi to canonical source under ``workspace/apps``.
The local Supervisor lifecycle wrappers remain for backward compatibility with
apps deployed before cloud deployment became authoritative.
"""

from __future__ import annotations

from ..agent.context import get_current_chat_id
from ..app_workspaces import (
    AppWorkspaceError,
    create_app_workspace,
    list_app_workspaces,
    resolve_app_workspace,
)
from .cloud_client import CloudDeployClient, CloudDeployError
from .store import ProjectStore
from .supervisor import Supervisor


def _store() -> ProjectStore:
    return ProjectStore()


def _sup() -> Supervisor:
    return Supervisor()


def _cloud() -> CloudDeployClient:
    return CloudDeployClient()


def app_create(name: str) -> dict:
    """Reserve a cloud app identity and its durable local source workspace."""
    try:
        app = _cloud().create_app(name)
    except CloudDeployError as exc:
        return {"error": str(exc)}

    app_id = app.get("id")
    if not isinstance(app_id, str):
        return {**app, "workspace_error": "Cloud response did not include an app ID"}
    try:
        workspace = create_app_workspace(
            app_id,
            str(app.get("name") or name),
            origin_chat_id=get_current_chat_id(),
        )
        return {**app, **workspace}
    except (AppWorkspaceError, OSError) as exc:
        return {**app, "workspace_error": str(exc)}


def app_workspace(app_id: str) -> dict:
    """Resolve or adopt an app's durable local source workspace."""
    try:
        return resolve_app_workspace(
            app_id,
            origin_chat_id=get_current_chat_id(),
        )
    except (AppWorkspaceError, OSError) as exc:
        return {"app_id": app_id, "found": False, "error": str(exc)}


def app_info(app_id: str) -> dict:
    """Get cloud app metadata, component deployment state, and active URLs."""
    try:
        return _cloud().get_app_info(app_id)
    except CloudDeployError as exc:
        return {"error": str(exc)}


def secrets_list() -> dict:
    """List user secret references and configured metadata, never values."""
    try:
        return _cloud().list_secret_metadata()
    except CloudDeployError as exc:
        return {"error": str(exc)}


def apps_list() -> dict:
    """List every durable local app workspace, including unfinished apps."""
    try:
        return list_app_workspaces()
    except (AppWorkspaceError, OSError) as exc:
        return {"error": str(exc)}


def legacy_apps_list() -> dict:
    """List device-local Supervisor apps kept for migration compatibility."""
    supervisor = _sup()
    return {
        "apps": [
            {
                "slug": project.slug,
                "status": project.status,
                "running": supervisor.is_running(project),
                "port": project.spec.port,
            }
            for project in _store().list()
        ]
    }


def app_status(slug: str) -> dict:
    """Get one deployed app's status (stored status, whether it's running, port)."""
    project = _store().get(slug)
    if project is None:
        return {"error": f"unknown app: {slug}"}
    return {
        "slug": slug,
        "status": project.status,
        "running": _sup().is_running(project),
        "port": project.spec.port,
    }


def app_logs(slug: str, tail: int = 100) -> dict:
    """Fetch recent container logs for a deployed app (to debug it)."""
    project = _store().get(slug)
    if project is None:
        return {"error": f"unknown app: {slug}"}
    return {"slug": slug, "logs": _sup().logs(project, tail=int(tail))}


def app_restart(slug: str) -> dict:
    """Restart a deployed app's container."""
    store = _store()
    project = store.get(slug)
    if project is None:
        return {"error": f"unknown app: {slug}"}
    sup = _sup()
    try:
        handle = sup.start(project)
    except Exception as exc:  # noqa: BLE001 - container backends raise varied errors
        store.set_status(slug, "error")
        return {"error": f"restart failed: {exc}"}
    ok, _ = sup.health(handle["url"])
    store.set_status(slug, "running" if ok else "error")
    return {
        "slug": slug,
        "status": "running" if ok else "error",
        "url": handle["url"],
        "health_ok": ok,
    }


def app_stop(slug: str) -> dict:
    """Stop a deployed app's container (keeps its definition so it can restart)."""
    store = _store()
    project = store.get(slug)
    if project is None:
        return {"error": f"unknown app: {slug}"}
    _sup().stop(project)
    store.set_status(slug, "stopped")
    return {"slug": slug, "status": "stopped"}
