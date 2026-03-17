import uuid

from aios_core.agent import create_agent
from aios_core.dream import dream
from aios_core.initialize import (
    CYAN,
    DIM,
    GREEN,
    RESET,
    register_runtime_shutdown,
    start_runtime,
)
from aios_core.sessions import save_chat_session
from agno.agent import RunEvent


def new_chat():
    if not messages:
        return

    chat_id = str(uuid.uuid4())
    save_chat_session(chat_id, messages)

    messages.clear()
    print(f"Chat saved: {chat_id}")

messages = []
start_runtime(start_heartbeat=False)
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