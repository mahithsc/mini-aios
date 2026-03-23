from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware

from aios_core.initialize import register_runtime_shutdown, shutdown_runtime, start_runtime
from server.runs.runtime import shutdown_runs_service, start_runs_service
from server.ws.connection import handle_websocket_connection

register_runtime_shutdown()


@asynccontextmanager
async def lifespan(_: FastAPI):
    start_runtime(start_heartbeat=False)
    await start_runs_service()
    try:
        yield
    finally:
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


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await handle_websocket_connection(websocket)
