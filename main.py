from __future__ import annotations

import os
<<<<<<< Updated upstream

import uvicorn
=======
import uuid
from datetime import datetime
from agent import create_agent, SKILLS_INDEX_PATH
from agno.agent import RunEvent
from crons import get_cron_manager
from dream import dream
from tools import RESET, DIM, CYAN, GREEN

SKILLS_DIR = os.path.dirname(SKILLS_INDEX_PATH)
SESSION_DIR = "session"
SESSION_MANIFEST_PATH = f"{SESSION_DIR}/session_manifest.json"
>>>>>>> Stashed changes


def main() -> None:
    os.environ.setdefault("AIOS_HEARTBEAT_ENABLED", "0")
    host = os.getenv("AIOS_SERVER_HOST", "0.0.0.0")
    port = int(os.getenv("AIOS_SERVER_PORT", "8765"))
    uvicorn.run("server.server:app", host=host, port=port, reload=False)


<<<<<<< Updated upstream
if __name__ == "__main__":
    main()
=======
def load_manifest():
    with open(SESSION_MANIFEST_PATH) as f:
        return json.load(f)


def save_manifest(manifest):
    with open(SESSION_MANIFEST_PATH, "w") as f:
        json.dump(manifest, f, indent=2)


def new_chat():
    if not messages:
        return

    chat_id = str(uuid.uuid4())
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"chat_{timestamp}.json"
    filepath = os.path.join(SESSION_DIR, filename)

    with open(filepath, "w") as f:
        json.dump(messages, f, indent=2)

    manifest = load_manifest()
    manifest.append({"id": chat_id, "file": filename, "status": "new"})
    save_manifest(manifest)

    messages.clear()
    print(f"Chat saved: {filename}")

messages = []
init()
get_cron_manager().start()
atexit.register(get_cron_manager().shutdown)

while True:
    content = ""
    user_input = input("> ")

    if user_input.strip() == "/new-chat":
        new_chat()
        continue

    if user_input.strip() == "/dream":
        dream()
        continue
    
    messages.append({"role": "user", "content": user_input})
    agent = create_agent()
    response = agent.run(messages, stream=True, stream_events=True)
    for event in response:
        if event.event == RunEvent.tool_call_started:
            tool = event.tool
            print(f"\n  {DIM}{CYAN}▶ {tool.tool_name}{RESET}{DIM}({tool.tool_args}){RESET}", flush=True)

        elif event.event == RunEvent.tool_call_completed:
            tool = event.tool
            result_preview = str(tool.result)[:120]
            print(f"  {DIM}{GREEN}✓ {tool.tool_name}{RESET}{DIM} → {result_preview}{RESET}", flush=True)

        elif event.event == RunEvent.run_content:
            if event.content is not None:
                content += event.content
                print(event.content, end="", flush=True)

    messages.append({"role": "assistant", "content": content})
    print()
>>>>>>> Stashed changes
