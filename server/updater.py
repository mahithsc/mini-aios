from __future__ import annotations

import os
import secrets
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException

from aios_core.db import get_db_connection
from aios_core.release import get_release_info
from aios_core.runtime_control import RuntimeDrainingError, get_runtime_control
from aios_core.workspace import ensure_workspace_dir
from server.execution.runtime import get_runs_service

router = APIRouter(prefix="/internal/updater", tags=["updater"])


def _expected_token() -> str | None:
    direct = os.getenv("AIOS_UPDATER_TOKEN")
    if direct:
        return direct.strip()
    token_path = Path(
        os.getenv("AIOS_UPDATER_TOKEN_FILE", "/run/secrets/aios-updater-token")
    )
    try:
        return token_path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


async def require_updater_token(
    authorization: str | None = Header(default=None),
    x_updater_token: str | None = Header(default=None),
) -> None:
    expected = _expected_token()
    if expected is None:
        raise HTTPException(status_code=503, detail="Updater API is not configured")
    provided = x_updater_token
    if authorization and authorization.startswith("Bearer "):
        provided = authorization.split(" ", 1)[1]
    if not provided or not secrets.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="Invalid updater token")


async def require_accepting_work() -> None:
    try:
        get_runtime_control().ensure_accepting_work()
    except RuntimeDrainingError as exc:
        raise HTTPException(
            status_code=503,
            detail=str(exc),
            headers={"Retry-After": "30"},
        ) from exc


def _active_runs() -> int:
    try:
        return len(get_runs_service().list_active_runs())
    except RuntimeError:
        return 0


@router.get("/live", dependencies=[])
async def updater_live(
    authorization: str | None = Header(default=None),
    x_updater_token: str | None = Header(default=None),
) -> dict[str, object]:
    await require_updater_token(authorization, x_updater_token)
    return {"status": "live", **get_release_info().as_dict()}


@router.get("/ready", dependencies=[])
async def updater_ready(
    authorization: str | None = Header(default=None),
    x_updater_token: str | None = Header(default=None),
) -> dict[str, object]:
    await require_updater_token(authorization, x_updater_token)
    workspace = ensure_workspace_dir()
    checks: dict[str, str] = {}
    status = "ready"
    try:
        with get_db_connection() as connection:
            result = connection.execute("PRAGMA quick_check").fetchone()
        checks["database"] = "ok" if result and result[0] == "ok" else "error"
    except Exception:
        checks["database"] = "error"
    try:
        required = (
            workspace,
            workspace / "skills",
            workspace / "session",
            workspace / "runs",
        )
        checks["runtimeDirectories"] = (
            "ok" if all(path.exists() and path.is_dir() for path in required) else "error"
        )
    except OSError:
        checks["runtimeDirectories"] = "error"
    if "error" in checks.values():
        status = "not_ready"
    drain = get_runtime_control().snapshot(active_runs=_active_runs())
    return {
        "status": status,
        **get_release_info().as_dict(),
        "migrationState": "complete",
        "acceptingWork": not drain.draining,
        "drain": drain.as_dict(),
        "checks": checks,
    }


@router.post("/drain", dependencies=[])
async def updater_drain(
    authorization: str | None = Header(default=None),
    x_updater_token: str | None = Header(default=None),
) -> dict[str, object]:
    await require_updater_token(authorization, x_updater_token)
    snapshot = get_runtime_control().request_drain().as_dict()
    snapshot["activeRuns"] = _active_runs()
    return snapshot


@router.get("/drain", dependencies=[])
async def updater_drain_status(
    authorization: str | None = Header(default=None),
    x_updater_token: str | None = Header(default=None),
) -> dict[str, object]:
    await require_updater_token(authorization, x_updater_token)
    return get_runtime_control().snapshot(active_runs=_active_runs()).as_dict()


@router.post("/resume", dependencies=[])
async def updater_resume(
    authorization: str | None = Header(default=None),
    x_updater_token: str | None = Header(default=None),
) -> dict[str, object]:
    await require_updater_token(authorization, x_updater_token)
    return get_runtime_control().resume().as_dict()
