from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from aios_core.integrations.gmail import (
    GmailIntegrationError,
    GmailOAuthService,
)
from server.auth import require_local_token

router = APIRouter(prefix="/integrations/gmail", tags=["integrations"])


def _service() -> GmailOAuthService:
    return GmailOAuthService()


class CompleteGmailConnectionRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    session_id: str = Field(alias="sessionId", min_length=16, max_length=256)
    code: str = Field(min_length=1, max_length=4096)
    state: str = Field(min_length=16, max_length=1024)


@router.get("", dependencies=[Depends(require_local_token)])
async def gmail_status() -> dict[str, object]:
    return _service().connection_status()


@router.post("/connect", dependencies=[Depends(require_local_token)])
async def connect_gmail() -> dict[str, object]:
    try:
        return await _service().start_authorization(services=("gmail",))
    except GmailIntegrationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/connect/complete", dependencies=[Depends(require_local_token)])
async def complete_gmail(
    body: CompleteGmailConnectionRequest,
) -> dict[str, object]:
    try:
        await _service().complete_authorization(
            session_id=body.session_id,
            code=body.code,
            state=body.state,
        )
    except GmailIntegrationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _service().connection_status()


@router.delete("", dependencies=[Depends(require_local_token)])
async def disconnect_gmail() -> dict[str, bool]:
    await _service().disconnect()
    return {"disconnected": True}
