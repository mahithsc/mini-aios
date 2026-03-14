import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from aios_core.initialize import register_runtime_shutdown, shutdown_runtime, start_runtime

register_runtime_shutdown()


@asynccontextmanager
async def lifespan(_: FastAPI):
    start_runtime()
    try:
        yield
    finally:
        shutdown_runtime()


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class MessageRequest(BaseModel):
    message: str


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/message")
async def post_message(_: MessageRequest):
    return {"reply": "pong"}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()

    try:
        while True:
            await websocket.receive_text()
            await websocket.send_text("pong")
    except WebSocketDisconnect:
        return


if __name__ == "__main__":
    try:
        import uvicorn
    except ImportError as exc:
        raise SystemExit(
            "uvicorn is required to run server.py. Install it with `uv add uvicorn`."
        ) from exc

    host = os.getenv("AIOS_SERVER_HOST", "127.0.0.1")
    port = int(os.getenv("AIOS_SERVER_PORT", "8765"))
    uvicorn.run("server:app", host=host, port=port, reload=False)
