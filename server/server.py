from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from aios_core.initialize import register_runtime_shutdown, shutdown_runtime, start_runtime
from server.notifications.runtime import shutdown_notification_service, start_notification_service
from server.runs.runtime import shutdown_runs_service, start_runs_service
from server.ws.connection import handle_websocket_connection
from server.tv_remote.api import router as samsung_tv_router

register_runtime_shutdown()


@asynccontextmanager
async def lifespan(_: FastAPI):
    start_runtime(start_heartbeat=False)
    await start_notification_service()
    await start_runs_service()
    try:
        yield
    finally:
        await shutdown_notification_service()
        await shutdown_runs_service()
        shutdown_runtime()


app = FastAPI(lifespan=lifespan)
# Expose endpoints both with and without a /api prefix so the UI works in:
# - Vite dev proxy mode (UI calls /api/*)
# - single-server mode (UI or other clients can call /tv/* directly)
app.include_router(samsung_tv_router)
app.include_router(samsung_tv_router, prefix="/api")
# Serve the built React UI (optional). After you run `npm run build` in tv-remote-ui,
# you can access the remote at http://<server>:8000/ or /remote.
_dist = Path(__file__).resolve().parent.parent / "tv-remote-ui" / "dist"
_assets = _dist / "assets"
if _assets.exists():
    app.mount("/assets", StaticFiles(directory=str(_assets)), name="assets")

    @app.get("/", include_in_schema=False)
    async def remote_index() -> FileResponse:
        return FileResponse(str(_dist / "index.html"))

    @app.get("/remote", include_in_schema=False)
    async def remote_index_alias() -> FileResponse:
        return FileResponse(str(_dist / "index.html"))

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await handle_websocket_connection(websocket)
