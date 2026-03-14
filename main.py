import json
import os
import uuid
from datetime import datetime

from aios_core.agent import create_agent
from aios_core.dream import dream
from aios_core.initialize import (
    CYAN,
    DIM,
    GREEN,
    RESET,
    SESSION_DIR,
    load_manifest,
    register_runtime_shutdown,
    save_manifest,
    start_runtime,
)
from agno.agent import RunEvent


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
start_runtime()
register_runtime_shutdown()

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