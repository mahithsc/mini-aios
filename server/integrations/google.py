from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from aios_core.integrations.google import (
    GoogleIntegrationError,
    GoogleOAuthService,
)
from server.auth import require_local_token

router = APIRouter(prefix="/integrations/google", tags=["integrations"])


class GoogleConnectionRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)


class StartGoogleConnectionRequest(GoogleConnectionRequest):
    services: list[str] = Field(
        default_factory=lambda: ["gmail", "calendar"],
        min_length=1,
        max_length=2,
    )
    client_profile: str | None = Field(
        default=None,
        alias="clientProfile",
        min_length=1,
        max_length=128,
    )


class CompleteGoogleConnectionRequest(GoogleConnectionRequest):
    session_id: str = Field(alias="sessionId", min_length=16, max_length=256)
    code: str = Field(min_length=1, max_length=4096)
    state: str = Field(min_length=16, max_length=1024)


class CancelGoogleConnectionRequest(GoogleConnectionRequest):
    session_id: str = Field(alias="sessionId", min_length=16, max_length=256)


def _service() -> GoogleOAuthService:
    return GoogleOAuthService()


def _http_error(exc: GoogleIntegrationError) -> HTTPException:
    status_code = 503 if exc.code in {"not_configured", "oauth_unavailable"} else 400
    return HTTPException(
        status_code=status_code,
        detail={"code": exc.code or "google_integration_error", "message": str(exc)},
    )


@router.get("", dependencies=[Depends(require_local_token)])
async def google_status() -> dict[str, object]:
    return _service().connection_status()


@router.post("/connect/start", dependencies=[Depends(require_local_token)])
async def start_google_connection(
    body: StartGoogleConnectionRequest,
) -> dict[str, object]:
    try:
        return await _service().start_authorization(
            services=body.services,
            client_profile=body.client_profile,
        )
    except GoogleIntegrationError as exc:
        raise _http_error(exc) from exc


@router.post("/connect/complete", dependencies=[Depends(require_local_token)])
async def complete_google_connection(
    body: CompleteGoogleConnectionRequest,
) -> dict[str, object]:
    try:
        await _service().complete_authorization(
            session_id=body.session_id,
            code=body.code,
            state=body.state,
        )
        return _service().connection_status()
    except GoogleIntegrationError as exc:
        raise _http_error(exc) from exc


@router.post("/connect/cancel", dependencies=[Depends(require_local_token)])
async def cancel_google_connection(
    body: CancelGoogleConnectionRequest,
) -> dict[str, object]:
    _service().cancel_authorization(session_id=body.session_id)
    return _service().connection_status()


@router.delete("", dependencies=[Depends(require_local_token)])
async def disconnect_google() -> dict[str, bool]:
    await _service().disconnect()
    return {"disconnected": True}
