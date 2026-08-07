from __future__ import annotations

from collections.abc import Sequence

from ..apps.coordinator import AppCoordinator
from ..apps.manifest import ManifestValidationError
from ..apps.registry import AppRegistryError
from ..apps.runtime import AppRuntimeError
from ..apps.service import AppSourceError, SnapshotError
from ..runtime_context import get_current_chat_id


def app(
    action: str,
    app_id: str | None = None,
    slug: str | None = None,
    name: str | None = None,
    description: str = "",
    version: str = "0.1.0",
    executable: str | None = None,
    args: Sequence[str] = (),
    approve_network: bool = False,
):
    """Create, validate, prepare, enable, and run isolated Apps.

    Args:
        action: create, register, list, inspect, validate, prepare, enable,
            disable, run, approve_network, revoke_network, or reset_data.
        app_id: App id or slug for every action except create/list/register.
        slug: Folder slug for create/register.
        name: Display name used by create.
        description: Description used by create.
        version: Initial version used by create.
        executable: Declared executable id for run.
        args: Extra argument array passed to the declared executable.
        approve_network: For prepare only; set true solely after the user
            explicitly approves App network access.
    """

    coordinator = AppCoordinator()
    normalized = (action or "").strip().lower()
    selector = (app_id or slug or "").strip()
    try:
        if normalized == "create":
            if not slug or not slug.strip():
                return {"error": "slug is required for create"}
            return coordinator.create(
                slug.strip(),
                name=name,
                description=description or "",
                version=version or "0.1.0",
                created_by_chat_id=get_current_chat_id(),
            )
        if normalized == "register":
            if not slug or not slug.strip():
                return {"error": "slug is required for register"}
            return coordinator.register(
                slug.strip(),
                created_by_chat_id=get_current_chat_id(),
            )
        if normalized == "list":
            return coordinator.list()
        if not selector:
            return {"error": "app_id is required for this action"}
        if normalized == "inspect":
            return coordinator.inspect(selector)
        if normalized == "validate":
            return coordinator.validate(selector)
        if normalized == "prepare":
            return coordinator.prepare(
                selector,
                approve_network=bool(approve_network),
            )
        if normalized == "enable":
            return coordinator.enable(selector)
        if normalized == "disable":
            return coordinator.disable(selector)
        if normalized == "run":
            if not executable or not executable.strip():
                return {"error": "executable is required for run"}
            if isinstance(args, (str, bytes)):
                return {"error": "args must be an array"}
            return coordinator.run(selector, executable.strip(), tuple(args or ()))
        if normalized == "approve_network":
            return coordinator.set_network_approved(selector, True)
        if normalized == "revoke_network":
            return coordinator.set_network_approved(selector, False)
        if normalized == "reset_data":
            return coordinator.reset_data(selector)
        return {
            "error": (
                "unknown action; use create, register, list, inspect, validate, "
                "prepare, enable, disable, run, approve_network, revoke_network, "
                "or reset_data"
            )
        }
    except (
        AppRegistryError,
        AppRuntimeError,
        AppSourceError,
        ManifestValidationError,
        SnapshotError,
        OSError,
        ValueError,
    ) as exc:
        return {"error": str(exc), "type": type(exc).__name__}
