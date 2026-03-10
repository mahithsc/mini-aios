import atexit
import json
import os
import uuid
from datetime import datetime
from agent import create_agent
from agno.agent import RunEvent
from crons import cron_manager
from dream import dream

RESET, BOLD, DIM, CYAN, GREEN, YELLOW = (
    "\033[0m", "\033[1m", "\033[2m", "\033[36m", "\033[32m", "\033[33m"
)

SKILLS_DIR = "skills"
SESSION_DIR = "session"
SESSION_MANIFEST_PATH = f"{SESSION_DIR}/session_manifest.json"
SKILLS_INDEX_PATH = f"{SKILLS_DIR}/skills_index.json"


def init():
    os.makedirs(SKILLS_DIR, exist_ok=True)
    os.makedirs(SESSION_DIR, exist_ok=True)

    files_to_create = {
        SESSION_MANIFEST_PATH: [],
        SKILLS_INDEX_PATH: [],
    }

    for path, default_content in files_to_create.items():
        if not os.path.exists(path):
            with open(path, "w") as f:
                json.dump(default_content, f, indent=2)


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
cron_manager.start()
atexit.register(cron_manager.shutdown)

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