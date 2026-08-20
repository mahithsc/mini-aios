"""Main-agent tools for cloud app identity and legacy local app lifecycle.

Deployed services outlive the Codex session that built them, so the main agent
needs to list / inspect / debug / restart / stop them. Thin wrappers over the
durable ProjectStore + Supervisor. ``_store``/``_sup`` are the injection seams
for tests.

Cloud app reservation and the artifact/deployment tools are intentionally
deterministic orchestration stubs while their production implementations are
developed. The artifact stub validates ``aios.deploy.yaml`` only far enough to
route its declared components; the stubs perform no cloud reservation,
filesystem cleanup, artifact upload, cloud deployment, status lookup, or
rollback. Every stub response is explicitly marked so it cannot be confused
with live infrastructure state.
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
    DeploymentReceiptMismatchError,
    DeploymentReceiptNotFoundError,
    InvalidArtifactHandoffError,
)
from .cloud_client import CloudDeployClient, CloudDeployError
from .disclosures import stub_deployment_evidence
from .manifest import ManifestValidationError, load_deployment_manifest
from .orchestration_state import StubDeploymentReceiptStore
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


_STUB_RECEIPTS = StubDeploymentReceiptStore()


def _receipts() -> StubDeploymentReceiptStore:
    return _STUB_RECEIPTS


_STUB_COMPONENTS = ("database", "server", "frontend")
_STUB_PUBLIC_COMPONENTS = ("server", "frontend")
_STUB_APPS_DOMAIN = "apps.winkapiserver.org"


def _stub_id(prefix: str, *values: object) -> str:
    """Return a stable opaque ID for an orchestration-only response."""

    material = "\0".join(str(value) for value in values).encode("utf-8")
    return f"{prefix}_stub{hashlib.sha256(material).hexdigest()[:16]}"


def _stub_response(
    *,
    message: str = "No artifact or cloud deployment operation was performed.",
    **values: object,
) -> dict:
    return {
        **values,
        "stubbed": True,
        "simulation": "orchestration_only",
        "message": message,
    }


def _artifact_error_response(
    error: ArtifactCreationError,
    *,
    handoff_id: str,
    **values: object,
) -> dict:
    """Translate a typed artifact failure into a model-actionable tool result."""

    return _stub_response(
        status="error",
        error_code=error.code,
        error=str(error),
        agent_instruction=error.agent_instruction,
        retryable=error.retryable,
        handoff_id=handoff_id,
        cleanup_status="stubbed_not_performed",
        **values,
    )


def _require_receipt(receipt_type: str, receipt_id: str) -> dict:
    getter = getattr(_receipts(), receipt_type)
    receipt = getter(receipt_id)
    if receipt is None:
        raise DeploymentReceiptNotFoundError(receipt_type, receipt_id)
    return receipt


def _dependency_error_response(error: ArtifactCreationError, **values: object) -> dict:
    return _stub_response(
        status="error",
        error_code=error.code,
        error=str(error),
        agent_instruction=error.agent_instruction,
        retryable=error.retryable,
        **values,
    )


def _artifact_evidence(artifact: dict) -> dict:
    return stub_deployment_evidence(
        worktree_path=str(artifact.get("workspace_path") or "") or None
    )


def app_create(name: str) -> dict:
    """Create a local app identity/workspace without reserving anything in cloud."""

    normalized_name = name.strip() if isinstance(name, str) else ""
    if not normalized_name:
        return _stub_response(status="error", error="name must not be empty")

    app_id = _stub_id("app", normalized_name, get_current_chat_id())
    try:
        workspace = create_app_workspace(
            app_id,
            normalized_name,
            origin_chat_id=get_current_chat_id(),
        )
    except (AppWorkspaceError, OSError) as exc:
        return _stub_response(
            status="error",
            id=app_id,
            app_id=app_id,
            name=normalized_name,
            workspace_error=str(exc),
        )
    payload = {"status": "ready", "id": app_id, **workspace}
    return _stub_response(
        message=(
            "Created a local durable app workspace; no cloud app identity was reserved."
        ),
        **payload,
    )


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
    """Simulate accepting a Codex handoff using its manifest for component routing."""

    # This small readiness gate is intentionally real even while artifact upload
    # remains stubbed. A reservation returned by codex_start is not deployable;
    # Codex must publish the registered worktree and exact commit first.
    try:
        try:
            handoff = _worktrees().get_handoff(handoff_id)
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
                "verification_status": "stubbed_manifest_rejected",
            }
        return _artifact_error_response(exc, handoff_id=handoff_id, **values)

    components = [
        component
        for component in _STUB_COMPONENTS
        if getattr(manifest, component) is not None
    ]

    receipt = {
        "artifact_id": _stub_id("art", handoff_id, app_id, source_commit),
        "handoff_id": handoff_id,
        "app_id": app_id,
        "workspace_path": workspace_path,
        "source_commit": source_commit,
        "components": components,
        "status": "ready",
    }
    _receipts().register_artifact(receipt)
    return _stub_response(
        status="ready",
        artifact_id=receipt["artifact_id"],
        handoff_id=handoff_id,
        app_id=app_id,
        workspace_path=workspace_path,
        source_commit=source_commit,
        components=components,
        verification_status="stubbed_not_verified",
        cleanup_status="stubbed_not_performed",
        **_artifact_evidence(receipt),
    )


def prepare_app_route(artifact_id: str) -> dict:
    """Simulate reserving the stable public route required by an artifact."""

    # TODO(issue-5 follow-up): Implement this in aios-cloud by persisting an
    # opaque per-app hostname, configuring wildcard edge routing, and making
    # the canonical origin available to the server runtime. See
    # docs/app-domain-routing-implementation-plan.md.
    try:
        artifact = _require_receipt("artifact", artifact_id)
    except ArtifactCreationError as exc:
        return _dependency_error_response(exc, artifact_id=artifact_id)

    app_id = str(artifact["app_id"])
    ordered = list(artifact["components"])
    public = [
        component for component in _STUB_PUBLIC_COMPONENTS if component in ordered
    ]
    if not public:
        return _stub_response(
            status="error",
            error="database-only artifacts do not require a public app route",
            **_artifact_evidence(artifact),
        )

    host_key = hashlib.sha256(app_id.encode("utf-8")).hexdigest()[:20]
    hostname = f"a-{host_key}.{_STUB_APPS_DOMAIN}"
    canonical_url = f"https://{hostname}"
    has_server = "server" in public
    has_frontend = "frontend" in public
    if has_frontend and has_server:
        routing_mode = "frontend_with_api_prefix"
        routes = {"/*": "frontend", "/api/*": "server"}
        api_base_url = f"{canonical_url}/api"
        cors_allowed_origins = [canonical_url]
    elif has_frontend:
        routing_mode = "frontend_only"
        routes = {"/*": "frontend"}
        api_base_url = None
        cors_allowed_origins = []
    else:
        routing_mode = "server_only"
        routes = {"/*": "server"}
        api_base_url = canonical_url
        cors_allowed_origins = []

    route_id = _stub_id("route", artifact_id)
    receipt = {
        "route_id": route_id,
        "artifact_id": artifact_id,
        "app_id": app_id,
        "components": ordered,
        "status": "ready",
    }
    _receipts().register_route(receipt)
    return _stub_response(
        message=(
            "Reserved a deterministic routing contract only; no DNS, TLS, edge, "
            "Vercel, or DigitalOcean configuration was performed."
        ),
        status="ready",
        route_id=route_id,
        artifact_id=artifact_id,
        app_id=app_id,
        components=ordered,
        hostname=hostname,
        canonical_url=canonical_url,
        api_base_url=api_base_url,
        routing_mode=routing_mode,
        routes=routes,
        cors_allowed_origins=cors_allowed_origins,
        provisioning_status="stubbed_not_performed",
        live=False,
        **_artifact_evidence(artifact),
    )


def deploy_app_artifact(
    artifact_id: str,
    route_id: str | None = None,
) -> dict:
    """Simulate enqueueing a deployment pipeline without contacting the cloud."""

    try:
        artifact = _require_receipt("artifact", artifact_id)
    except ArtifactCreationError as exc:
        return _dependency_error_response(exc, artifact_id=artifact_id)

    app_id = str(artifact["app_id"])
    requested = list(artifact["components"])
    has_public_component = any(
        component in _STUB_PUBLIC_COMPONENTS for component in requested
    )
    if has_public_component:
        try:
            route = _require_receipt("route", route_id or "")
            if route["artifact_id"] != artifact_id:
                raise DeploymentReceiptMismatchError(
                    "The route receipt belongs to a different artifact."
                )
        except ArtifactCreationError as exc:
            return _dependency_error_response(
                exc, artifact_id=artifact_id, route_id=route_id
            )
    elif route_id is not None:
        return _stub_response(
            status="error",
            error="route_id must be omitted for database-only artifacts",
        )
    requested = [component for component in _STUB_COMPONENTS if component in requested]
    pipeline_id = _stub_id("pipe", app_id, artifact_id, route_id, *requested)
    deployments = [
        {
            "component": component,
            "deployment_id": _stub_id("dep", pipeline_id, component),
            "status": "active",
            "stubbed": True,
        }
        for component in requested
    ]
    receipt = {
        "pipeline_id": pipeline_id,
        "status": "active",
        "app_id": app_id,
        "artifact_id": artifact_id,
        "route_id": route_id,
        "components": requested,
        "deployments": deployments,
    }
    _receipts().register_pipeline(receipt)
    return _stub_response(
        id=pipeline_id,
        pipeline_id=pipeline_id,
        status="active",
        app_id=app_id,
        artifact_id=artifact_id,
        route_id=route_id,
        components=requested,
        deployments=deployments,
        **_artifact_evidence(artifact),
    )


def app_deployment_status(app_id: str) -> dict:
    """Return a simulated completed app deployment snapshot."""

    pipeline = _receipts().latest_pipeline(app_id)
    if pipeline is None:
        error = DeploymentReceiptNotFoundError("pipeline", app_id)
        return _dependency_error_response(error, app_id=app_id)
    return _stub_response(
        status="active",
        phase="active",
        app_id=app_id,
        pipeline_id=pipeline["pipeline_id"],
        **_artifact_evidence(
            _require_receipt("artifact", str(pipeline["artifact_id"]))
        ),
    )


def deployment_pipeline_status(pipeline_id: str) -> dict:
    """Return a simulated completed pipeline snapshot."""

    try:
        pipeline = _require_receipt("pipeline", pipeline_id)
    except ArtifactCreationError as exc:
        return _dependency_error_response(exc, pipeline_id=pipeline_id)
    return _stub_response(
        id=pipeline_id,
        pipeline_id=pipeline_id,
        status=pipeline["status"],
        app_id=pipeline["app_id"],
        **_artifact_evidence(
            _require_receipt("artifact", str(pipeline["artifact_id"]))
        ),
    )


def deployment_status(deployment_id: str) -> dict:
    """Return a simulated completed component deployment snapshot."""

    try:
        deployment = _require_receipt("deployment", deployment_id)
    except ArtifactCreationError as exc:
        return _dependency_error_response(exc, deployment_id=deployment_id)
    return _stub_response(
        id=deployment_id,
        deployment_id=deployment_id,
        status="active",
        component=deployment["component"],
        pipeline_id=deployment["pipeline_id"],
        url=None,
        **_artifact_evidence(
            _require_receipt("artifact", str(deployment["artifact_id"]))
        ),
    )


def deployment_events(deployment_id: str, after: int = -1) -> dict:
    """Return one simulated terminal event for orchestration tests."""

    try:
        deployment = _require_receipt("deployment", deployment_id)
    except ArtifactCreationError as exc:
        return _dependency_error_response(exc, deployment_id=deployment_id)
    cursor = int(after)
    events = []
    if cursor < 0:
        events = [
            {
                "cursor": 0,
                "type": "simulation.completed",
                "message": "Orchestration stub completed; no deployment occurred.",
            }
        ]
    return _stub_response(
        deployment_id=deployment_id,
        after=cursor,
        next_cursor=0,
        events=events,
        **_artifact_evidence(
            _require_receipt("artifact", str(deployment["artifact_id"]))
        ),
    )


def activate_app_route(app_id: str, route_id: str, pipeline_id: str) -> dict:
    """Simulate atomically pointing a stable app route at a completed pipeline."""

    # TODO(issue-5 follow-up): The real control plane must resolve provider
    # targets from pipeline_id, require every declared public component to be
    # healthy/active, and atomically switch the route registry. It must never
    # accept provider URLs supplied by the model.
    if not all(
        isinstance(value, str) and value.strip()
        for value in (app_id, route_id, pipeline_id)
    ):
        return _stub_response(
            status="error",
            error="app_id, route_id, and pipeline_id are required",
        )
    try:
        route = _require_receipt("route", route_id)
        pipeline = _require_receipt("pipeline", pipeline_id)
        if (
            route["app_id"] != app_id
            or pipeline["app_id"] != app_id
            or route["artifact_id"] != pipeline["artifact_id"]
        ):
            raise DeploymentReceiptMismatchError(
                "The app, route, and pipeline receipts do not belong to one flow."
            )
    except ArtifactCreationError as exc:
        return _dependency_error_response(
            exc, app_id=app_id, route_id=route_id, pipeline_id=pipeline_id
        )
    return _stub_response(
        message=(
            "Route activation was simulated; the hostname is not live and no "
            "routing target was changed."
        ),
        status="active",
        app_id=app_id,
        route_id=route_id,
        pipeline_id=pipeline_id,
        activation_status="stubbed_not_performed",
        live=False,
        **_artifact_evidence(
            _require_receipt("artifact", str(pipeline["artifact_id"]))
        ),
    )


def app_route_status(app_id: str, route_id: str) -> dict:
    """Return simulated route state for orchestration sequence testing."""

    try:
        route = _require_receipt("route", route_id)
        if route["app_id"] != app_id:
            raise DeploymentReceiptMismatchError(
                "The route receipt belongs to a different app."
            )
    except ArtifactCreationError as exc:
        return _dependency_error_response(exc, app_id=app_id, route_id=route_id)
    return _stub_response(
        status="active",
        app_id=app_id,
        route_id=route_id,
        provisioning_status="stubbed_not_performed",
        live=False,
        **_artifact_evidence(_require_receipt("artifact", str(route["artifact_id"]))),
    )


def rollback_app_artifact(deployment_id: str) -> dict:
    """Simulate accepting an immutable artifact rollback request."""

    try:
        deployment = _require_receipt("deployment", deployment_id)
    except ArtifactCreationError as exc:
        return _dependency_error_response(exc, deployment_id=deployment_id)
    return _stub_response(
        id=_stub_id("rollback", deployment_id),
        source_deployment_id=deployment_id,
        artifact_id=deployment["artifact_id"],
        status="active",
        **_artifact_evidence(
            _require_receipt("artifact", str(deployment["artifact_id"]))
        ),
    )


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
