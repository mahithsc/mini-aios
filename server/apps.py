from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from aios_core.apps.coordinator import AppCoordinator
from aios_core.apps.manifest import ManifestValidationError
from aios_core.apps.models import AppOrigin
from aios_core.apps.registry import (
    AppConflictError,
    AppLifecycleError,
    AppNotFoundError,
    AppRegistryError,
)
from aios_core.apps.runtime import AppRuntimeError, DockerUnavailableError
from aios_core.apps.service import AppSourceError, SnapshotError
from server.auth import require_local_token
from server.updater import require_accepting_work

router = APIRouter(
    prefix="/apps",
    tags=["apps"],
    dependencies=[Depends(require_local_token)],
)


class _Request(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")


class CreateAppRequest(_Request):
    slug: str
    name: str | None = None
    description: str = ""
    version: str = "0.1.0"
    origin: AppOrigin = AppOrigin.USER


class PrepareAppRequest(_Request):
    approve_network: bool = Field(default=False, alias="approveNetwork")


class NetworkApprovalRequest(_Request):
    approved: bool


class RunExecutableRequest(_Request):
    args: list[str] = Field(default_factory=list)


def _coordinator() -> AppCoordinator:
    return AppCoordinator()


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, AppNotFoundError):
        status_code = 404
    elif isinstance(exc, (AppConflictError, AppLifecycleError)):
        status_code = 409
    elif isinstance(
        exc,
        (AppSourceError, ManifestValidationError, SnapshotError, ValueError),
    ):
        status_code = 422
    elif isinstance(exc, DockerUnavailableError):
        status_code = 503
    elif isinstance(exc, (AppRegistryError, AppRuntimeError)):
        status_code = 409
    else:
        status_code = 500
    return HTTPException(
        status_code=status_code,
        detail={"code": type(exc).__name__, "message": str(exc)},
    )


@router.get("")
async def list_apps() -> dict[str, object]:
    try:
        return _coordinator().list()
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("", dependencies=[Depends(require_accepting_work)])
async def create_app(body: CreateAppRequest) -> dict[str, object]:
    try:
        return _coordinator().create(
            body.slug,
            name=body.name,
            description=body.description,
            version=body.version,
            origin=body.origin,
        )["app"]
    except Exception as exc:
        raise _http_error(exc) from exc


@router.get("/{app_id}")
async def inspect_app(app_id: str) -> dict[str, object]:
    try:
        return _coordinator().inspect(app_id)
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/{app_id}/validate", dependencies=[Depends(require_accepting_work)])
async def validate_app(app_id: str) -> dict[str, object]:
    try:
        return _coordinator().validate(app_id)
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/{app_id}/prepare", dependencies=[Depends(require_accepting_work)])
async def prepare_app(
    app_id: str,
    body: PrepareAppRequest | None = None,
) -> dict[str, object]:
    try:
        result = _coordinator().prepare(
            app_id,
            approve_network=body.approve_network if body else False,
        )
        runtime = result.get("runtime")
        if isinstance(runtime, dict) and runtime.get("ok") is False:
            raise AppLifecycleError(
                str(
                    runtime.get("stderr")
                    or runtime.get("stdout")
                    or "preparation failed"
                )
            )
        return result
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/{app_id}/enable", dependencies=[Depends(require_accepting_work)])
async def enable_app(app_id: str) -> dict[str, object]:
    try:
        return _coordinator().enable(app_id)["app"]
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/{app_id}/disable", dependencies=[Depends(require_accepting_work)])
async def disable_app(app_id: str) -> dict[str, object]:
    try:
        return _coordinator().disable(app_id)["app"]
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/{app_id}/network", dependencies=[Depends(require_accepting_work)])
async def set_app_network(
    app_id: str,
    body: NetworkApprovalRequest,
) -> dict[str, object]:
    try:
        return _coordinator().set_network_approved(app_id, body.approved)["app"]
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post(
    "/{app_id}/executables/{executable_id}/run",
    dependencies=[Depends(require_accepting_work)],
)
async def run_app_executable(
    app_id: str,
    executable_id: str,
    body: RunExecutableRequest | None = None,
) -> dict[str, object]:
    try:
        return _coordinator().run(
            app_id,
            executable_id,
            body.args if body else (),
        )
    except Exception as exc:
        raise _http_error(exc) from exc


@router.delete("/{app_id}", dependencies=[Depends(require_accepting_work)])
async def unregister_app(app_id: str) -> dict[str, object]:
    try:
        return _coordinator().unregister(app_id)
    except Exception as exc:
        raise _http_error(exc) from exc


@router.delete("/{app_id}/data", dependencies=[Depends(require_accepting_work)])
async def reset_app_data(app_id: str) -> dict[str, object]:
    try:
        return _coordinator().reset_data(app_id)
    except Exception as exc:
        raise _http_error(exc) from exc
