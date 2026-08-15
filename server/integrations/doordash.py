from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from aios_core.integrations.doordash import (
    DoorDashConnectionService,
    DoorDashIntegrationError,
)
from server.auth import require_local_token

router = APIRouter(prefix="/integrations/doordash", tags=["integrations"])


def _service() -> DoorDashConnectionService:
    return DoorDashConnectionService()


def _http_error(exc: DoorDashIntegrationError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={
            "code": exc.code,
            "message": str(exc),
        },
    )


@router.get("", dependencies=[Depends(require_local_token)])
async def doordash_status() -> dict[str, object]:
    return _service().connection_status()


@router.post("/connect", dependencies=[Depends(require_local_token)])
async def connect_doordash() -> dict[str, object]:
    """Run dd-cli's browser login and return after it reaches the keychain."""

    try:
        return await _service().connect()
    except DoorDashIntegrationError as exc:
        raise _http_error(exc) from exc


@router.delete("", dependencies=[Depends(require_local_token)])
async def disconnect_doordash() -> dict[str, object]:
    return await _service().disconnect()
