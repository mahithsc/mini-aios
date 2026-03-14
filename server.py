import asyncio
import os
import threading
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from agno.agent import RunEvent
from aios_core.agent import create_agent
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


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)


def _serialize_event(event: Any) -> dict[str, Any] | None:
    if event.event == RunEvent.tool_call_started:
        tool = event.tool
        return {
            "type": "tool_call_started",
            "tool_name": tool.tool_name,
            "tool_args": _json_safe(tool.tool_args),
        }

    if event.event == RunEvent.tool_call_completed:
        tool = event.tool
        return {
            "type": "tool_call_completed",
            "tool_name": tool.tool_name,
            "tool_args": _json_safe(tool.tool_args),
            "result": _json_safe(tool.result),
        }

    if event.event == RunEvent.run_content and event.content is not None:
        return {
            "type": "content_delta",
            "content": event.content,
        }

    return None


def _run_agent(conversation: list[dict[str, str]]) -> tuple[list[dict[str, Any]], str]:
    agent = create_agent()
    events: list[dict[str, Any]] = []
    content_parts: list[str] = []
    response = agent.run(conversation, stream=True, stream_events=True)

    for event in response:
        serialized = _serialize_event(event)
        if serialized is None:
            continue
        events.append(serialized)
        if serialized["type"] == "content_delta":
            content_parts.append(serialized["content"])

    return events, "".join(content_parts)


async def _stream_agent_events(
    websocket: WebSocket, conversation: list[dict[str, str]]
) -> str:
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    def worker() -> None:
        try:
            events, assistant_content = _run_agent(conversation)
            for event in events:
                loop.call_soon_threadsafe(queue.put_nowait, event)
            loop.call_soon_threadsafe(
                queue.put_nowait,
                {
                    "type": "run_completed",
                    "content": assistant_content,
                },
            )
        except Exception as exc:  # pragma: no cover - defensive websocket boundary
            loop.call_soon_threadsafe(
                queue.put_nowait,
                {
                    "type": "error",
                    "message": str(exc),
                },
            )
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    threading.Thread(target=worker, daemon=True).start()

    assistant_content = ""
    while True:
        event = await queue.get()
        if event is None:
            break
        if event["type"] == "content_delta":
            assistant_content += str(event["content"])
        await websocket.send_json(event)

    return assistant_content


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/message")
async def post_message(request: MessageRequest):
    conversation = [{"role": "user", "content": request.message}]
    events, reply = await asyncio.to_thread(_run_agent, conversation)
    conversation.append({"role": "assistant", "content": reply})
    return {"events": events, "reply": reply}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    conversation: list[dict[str, str]] = []

    try:
        while True:
            message = (await websocket.receive_text()).strip()
            if not message:
                await websocket.send_json(
                    {"type": "error", "message": "Message cannot be empty."}
                )
                continue

            conversation.append({"role": "user", "content": message})
            await websocket.send_json({"type": "user_message", "content": message})
            reply = await _stream_agent_events(websocket, conversation)
            conversation.append({"role": "assistant", "content": reply})
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
