from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from aios_core.initialize import register_runtime_shutdown, shutdown_runtime, start_runtime
from aios_core.sessions import get_chat_artifacts_dir
from aios_core.workspace import resolve_workspace_path
from server.notifications.runtime import shutdown_notification_service, start_notification_service
from server.execution.runtime import shutdown_runs_service, start_runs_service
from server.lights import lights
from server.uploads import save_uploads
from server.ws.connection import handle_websocket_connection

register_runtime_shutdown()


@asynccontextmanager
async def lifespan(_: FastAPI):
    start_runtime(start_heartbeat=False)
    await lights.start()
    await start_notification_service()
    await start_runs_service()
    try:
        yield
    finally:
        await lights.shutdown()
        await shutdown_notification_service()
        await shutdown_runs_service()
        shutdown_runtime()


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/attachments")
async def upload_attachments(
    chatId: str = Form(...),
    files: list[UploadFile] = File(...),
) -> dict[str, object]:
    attachments = await save_uploads(chatId, files)
    return {"attachments": [attachment.model_dump(mode="json") for attachment in attachments]}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await handle_websocket_connection(websocket)


@app.get("/session-artifacts/{chat_id}/{artifact_path:path}")
async def get_session_artifact_file(chat_id: str, artifact_path: str) -> FileResponse:
    artifacts_root = get_chat_artifacts_dir(chat_id).resolve()
    requested_relative_path = Path(artifact_path)
    requested_path = (artifacts_root / requested_relative_path).resolve()

    try:
        requested_path.relative_to(artifacts_root)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Artifact not found.") from exc

    if not requested_path.exists() or not requested_path.is_file():
        raise HTTPException(status_code=404, detail="Artifact not found.")

    return FileResponse(requested_path)


# Serve static app files from workspace/apps/ directory.
# A static app at workspace/apps/<slug>/src/index.html is accessible at
# http://localhost:8765/apps/<slug>/src/index.html
_apps_dir = resolve_workspace_path("apps")
_apps_dir.mkdir(parents=True, exist_ok=True)
app.mount("/apps", StaticFiles(directory=str(_apps_dir), html=True), name="apps")
