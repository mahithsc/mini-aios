import uuid

from agents import RunConfig, Runner

from aios_core.agent import create_agent
from aios_core.dream import dream
from aios_core.initialize import (
    register_runtime_shutdown,
    start_runtime,
)
from aios_core.sessions import save_chat_session


def new_chat():
    if not messages:
        return

    chat_id = str(uuid.uuid4())
    save_chat_session(chat_id, messages)

    messages.clear()
    print(f"Chat saved: {chat_id}")

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
    response = Runner.run_sync(
        agent,
        messages,
        max_turns=None,
        run_config=RunConfig(tracing_disabled=True),
    )
    content = str(response.final_output or "")
    print(content, end="", flush=True)

    messages.append({"role": "assistant", "content": content})
    print()
