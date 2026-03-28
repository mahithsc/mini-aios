from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, UploadFile, WebSocket
from fastapi.middleware.cors import CORSMiddleware

from aios_core.initialize import register_runtime_shutdown, shutdown_runtime, start_runtime
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
