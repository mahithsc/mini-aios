"""Main-agent tools for cloud app identity and legacy local app lifecycle.

Deployed services outlive the Codex session that built them, so the main agent
needs to list / inspect / debug / restart / stop them. Thin wrappers over the
durable ProjectStore + Supervisor. ``_store``/``_sup`` are the injection seams
for tests.

Artifact and deployment operations are owned by the main agent. Codex prepares
an immutable registered handoff; these tools validate and upload it, enqueue the
cloud pipeline, and expose status without accepting model-supplied source paths,
revisions, component lists, or provider targets.
"""

from __future__ import annotations

import hashlib

from ..app_workspaces import (
    AppWorkspaceError,
    create_app_workspace,
    list_app_workspaces,
    resolve_app_workspace,
)
from ..runtime_context import get_current_chat_id
from .artifact_errors import (
    ArtifactCreationError,
    ArtifactHandoffNotReadyError,
    ArtifactManifestRejectedError,
    DeploymentReceiptNotFoundError,
    InvalidArtifactHandoffError,
)
from .cloud_client import CloudDeployClient, CloudDeployError
from .handoff_artifacts import (
    ArtifactHandoffReceipt,
    create_uploaded_artifact_from_handoff,
    load_artifact_handoff_receipt,
)
from .manifest import ManifestValidationError, load_deployment_manifest
from .store import ProjectStore
from .supervisor import Supervisor
from .worktree_handoff import (
    WorktreeHandoffError,
    WorktreeRegistry,
    WorktreeStatus,
)


def _store() -> ProjectStore:
    return ProjectStore()


def _sup() -> Supervisor:
    return Supervisor()


def _cloud() -> CloudDeployClient:
    return CloudDeployClient()


def _worktrees() -> WorktreeRegistry:
    return WorktreeRegistry()


def _artifact_error_response(
    error: ArtifactCreationError,
    *,
    handoff_id: str,
    **values: object,
) -> dict:
    """Translate a typed artifact failure into a model-actionable tool result."""

    return {
        "status": "error",
        "error_code": error.code,
        "error": str(error),
        "agent_instruction": error.agent_instruction,
        "retryable": error.retryable,
        "handoff_id": handoff_id,
        "cleanup_status": "not_completed",
        **values,
    }


def _uploaded_artifact(artifact_id: str) -> ArtifactHandoffReceipt:
    try:
        return load_artifact_handoff_receipt(
            registry=_worktrees(),
            artifact_id=artifact_id,
        )
    except WorktreeHandoffError as exc:
        raise DeploymentReceiptNotFoundError("artifact", artifact_id) from exc


def _dependency_error_response(error: ArtifactCreationError, **values: object) -> dict:
    return {
        "status": "error",
        "error_code": error.code,
        "error": str(error),
        "agent_instruction": error.agent_instruction,
        "retryable": error.retryable,
        **values,
    }


def app_create(name: str) -> dict:
    """Reserve a cloud app identity and create its canonical local workspace."""

    normalized_name = name.strip() if isinstance(name, str) else ""
    if not normalized_name:
        return {"status": "error", "error": "name must not be empty"}

    try:
        creation_material = (
            f"{get_current_chat_id()}\0{normalized_name.casefold()}".encode("utf-8")
        )
        idempotency_key = "main-agent-app:" + hashlib.sha256(
            creation_material
        ).hexdigest()
        cloud_app = _cloud().create_app(
            normalized_name,
            idempotency_key=idempotency_key,
        )
        app_id = cloud_app.get("id")
        if not isinstance(app_id, str) or not app_id.startswith("app_"):
            raise CloudDeployError("Cloud returned an invalid app identity")
        workspace = create_app_workspace(
            app_id,
            normalized_name,
            origin_chat_id=get_current_chat_id(),
        )
    except CloudDeployError as exc:
        return {"status": "error", "error": str(exc), "retryable": True}
    except (AppWorkspaceError, OSError) as exc:
        cleanup_error: str | None = None
        try:
            _cloud().delete_app(app_id)
        except CloudDeployError as cleanup_exc:
            cleanup_error = str(cleanup_exc)
        return {
            "status": "error",
            "id": app_id,
            "app_id": app_id,
            "name": normalized_name,
            "workspace_error": str(exc),
            "cloud_cleanup_error": cleanup_error,
            "retryable": cleanup_error is None,
        }
    return {
        "status": "ready",
        "id": app_id,
        "app_id": app_id,
        "cloud_app": cloud_app,
        **workspace,
    }


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


def create_app_artifact(handoff_id: str) -> dict:
    """Validate, upload, and clean a completed Codex workspace handoff."""

    registry = _worktrees()
    try:
        try:
            handoff = registry.get_handoff(handoff_id)
        except WorktreeHandoffError as exc:
            raise InvalidArtifactHandoffError(str(exc)) from exc

        if handoff.status != WorktreeStatus.HANDOFF_READY:
            raise ArtifactHandoffNotReadyError(handoff.status)
        if handoff.source_commit is None:
            raise InvalidArtifactHandoffError(
                "The ready Codex handoff does not contain a source commit."
            )
        app_id = handoff.app_id
        workspace_path = handoff.path
        source_commit = handoff.source_commit

        try:
            manifest = load_deployment_manifest(handoff.path)
        except (ManifestValidationError, OSError) as exc:
            raise ArtifactManifestRejectedError(str(exc)) from exc

        if manifest.app_id != app_id:
            raise ArtifactManifestRejectedError(
                "aios.deploy.yaml app_id does not match the artifact handoff: "
                f"expected {app_id}, found {manifest.app_id}"
            )
    except ArtifactCreationError as exc:
        values: dict[str, object] = {}
        if isinstance(exc, ArtifactManifestRejectedError):
            values = {
                "app_id": locals().get("app_id"),
                "workspace_path": locals().get("workspace_path"),
                "source_commit": locals().get("source_commit"),
                "verification_status": "manifest_rejected",
            }
        return _artifact_error_response(exc, handoff_id=handoff_id, **values)

    try:
        uploaded = create_uploaded_artifact_from_handoff(
            registry=registry,
            cloud=_cloud(),
            handoff_id=handoff_id,
        )
    except (WorktreeHandoffError, CloudDeployError, OSError) as exc:
        return {
            "status": "error",
            "error_code": "artifact_creation_failed",
            "error": str(exc),
            "agent_instruction": (
                "Stop this deployment chain. Inspect the handoff registry and cloud "
                "artifact error; retry only when the same handoff remains safely "
                "claimable or start a new Codex handoff."
            ),
            "retryable": False,
            "handoff_id": handoff_id,
            "app_id": app_id,
            "workspace_path": workspace_path,
            "source_commit": source_commit,
            "cleanup_status": "failed_or_unknown",
        }

    receipt = uploaded.model_dump(mode="json")
    return {
        **receipt,
        "status": "ready",
        "workspace_path": workspace_path,
        "artifact_created": True,
        "artifact_uploaded": True,
        "artifact_verified": True,
        "worktree_removed": uploaded.cleanup_status == WorktreeStatus.REMOVED,
        "stubbed": False,
    }


def prepare_app_route(artifact_id: str) -> dict:
    """Provision the stable public route required by an uploaded artifact."""

    try:
        artifact = _uploaded_artifact(artifact_id)
    except ArtifactCreationError as exc:
        return _dependency_error_response(exc, artifact_id=artifact_id)

    public = [
        component
        for component in ("server", "frontend")
        if component in artifact.components
    ]
    if not public:
        return {
            "status": "error",
            "error": "database-only artifacts do not require a public app route",
            "artifact_id": artifact_id,
        }
    try:
        route = _cloud().prepare_app_route(
            app_id=artifact.app_id,
            artifact_id=artifact.artifact_id,
        )
    except CloudDeployError as exc:
        return {"status": "error", "artifact_id": artifact_id, "error": str(exc)}
    return {
        **route,
        "route_id": route.get("id"),
        "status": "ready" if route.get("state") in {"ready", "active"} else route.get("state"),
        "components": artifact.components,
        "stubbed": False,
    }


def deploy_app_artifact(
    artifact_id: str,
    route_id: str | None = None,
) -> dict:
    """Enqueue the exact uploaded artifact as an ordered cloud pipeline."""

    try:
        artifact = _uploaded_artifact(artifact_id)
    except ArtifactCreationError as exc:
        return _dependency_error_response(exc, artifact_id=artifact_id)

    app_id = artifact.app_id
    requested = [
        component
        for component in ("database", "server", "frontend")
        if component in artifact.components
    ]
    has_public_component = any(
        component in {"server", "frontend"} for component in requested
    )
    if has_public_component:
        if not route_id:
            error = DeploymentReceiptNotFoundError("route", "")
            return _dependency_error_response(
                error, artifact_id=artifact_id, route_id=route_id
            )
        try:
            route = _cloud().get_app_route(app_id=app_id, route_id=route_id)
        except CloudDeployError as exc:
            return {
                "status": "error",
                "artifact_id": artifact_id,
                "route_id": route_id,
                "error": str(exc),
            }
        if route.get("artifact_id") != artifact_id:
            return {
                "status": "error",
                "error_code": "deployment_receipt_mismatch",
                "error": "The route belongs to a different artifact.",
                "artifact_id": artifact_id,
                "route_id": route_id,
            }
        if route.get("provisioning_status") != "provisioned":
            return {
                "status": "error",
                "error": "The app route is not provisioned.",
                "artifact_id": artifact_id,
                "route_id": route_id,
            }
    elif route_id is not None:
        return {
            "status": "error",
            "error": "route_id must be omitted for database-only artifacts",
        }
    try:
        pipeline = _cloud().enqueue_pipeline(
            app_id=app_id,
            artifact_id=artifact_id,
            components=requested,
            idempotency_key=f"main-agent:{artifact_id}:{route_id or 'database-only'}",
        )
    except CloudDeployError as exc:
        return {"status": "error", "artifact_id": artifact_id, "error": str(exc)}
    deployments = [
        {**item, "deployment_id": item.get("id")}
        for item in pipeline.get("deployments", [])
        if isinstance(item, dict)
    ]
    return {
        **pipeline,
        "pipeline_id": pipeline.get("id"),
        "route_id": route_id,
        "components": requested,
        "deployments": deployments,
        "stubbed": False,
    }


def app_deployment_status(app_id: str) -> dict:
    """Return the durable cloud deployment snapshot for an app."""

    try:
        return _cloud().check_app_status(app_id)
    except CloudDeployError as exc:
        return {"status": "error", "app_id": app_id, "error": str(exc)}


def deployment_pipeline_status(pipeline_id: str) -> dict:
    """Return the durable cloud deployment-pipeline snapshot."""

    try:
        return _cloud().get_deployment_pipeline(pipeline_id)
    except CloudDeployError as exc:
        return {"status": "error", "pipeline_id": pipeline_id, "error": str(exc)}


def deployment_status(deployment_id: str) -> dict:
    """Return a durable cloud component-deployment snapshot."""

    try:
        return _cloud().get_deployment(deployment_id)
    except CloudDeployError as exc:
        return {
            "status": "error",
            "deployment_id": deployment_id,
            "error": str(exc),
        }


def deployment_events(deployment_id: str, after: int = -1) -> dict:
    """Return durable cloud deployment events after a cursor."""

    try:
        return _cloud().get_deployment_events(deployment_id, after=int(after))
    except (CloudDeployError, TypeError, ValueError) as exc:
        return {
            "status": "error",
            "deployment_id": deployment_id,
            "after": after,
            "error": str(exc),
        }


def activate_app_route(app_id: str, route_id: str, pipeline_id: str) -> dict:
    """Atomically point a stable app route at a completed cloud pipeline."""

    if not all(
        isinstance(value, str) and value.strip()
        for value in (app_id, route_id, pipeline_id)
    ):
        return {
            "status": "error",
            "error": "app_id, route_id, and pipeline_id are required",
        }
    try:
        result = _cloud().activate_app_route(
            app_id=app_id,
            route_id=route_id,
            pipeline_id=pipeline_id,
        )
    except CloudDeployError as exc:
        return {
            "status": "error",
            "app_id": app_id,
            "route_id": route_id,
            "pipeline_id": pipeline_id,
            "error": str(exc),
        }
    return {
        **result,
        "route_id": result.get("id", route_id),
        "pipeline_id": pipeline_id,
        "status": result.get("state"),
        "stubbed": False,
    }


def app_route_status(app_id: str, route_id: str) -> dict:
    """Return owner-scoped durable route state from the cloud control plane."""

    try:
        result = _cloud().get_app_route(app_id=app_id, route_id=route_id)
    except CloudDeployError as exc:
        return {
            "status": "error",
            "app_id": app_id,
            "route_id": route_id,
            "error": str(exc),
        }
    return {
        **result,
        "route_id": result.get("id", route_id),
        "status": result.get("state"),
        "stubbed": False,
    }


def rollback_app_artifact(deployment_id: str) -> dict:
    """Queue a cloud rollback to a prior immutable deployment artifact."""

    try:
        return _cloud().rollback_deployment(deployment_id)
    except CloudDeployError as exc:
        return {
            "status": "error",
            "deployment_id": deployment_id,
            "error": str(exc),
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
    except Exception as exc:  # noqa: BLE001 - lifecycle tool returns structured errors
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
