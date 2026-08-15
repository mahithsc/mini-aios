import json

from .agent import create_agent
from .prompt_loader import load_prompt
from .sessions import list_chat_history, load_chat_session

DREAM_PROMPT = load_prompt("dream.md")
_MAX_DREAM_CHATS = 10
_MAX_MESSAGES_PER_CHAT = 40
_MAX_DREAM_CONTEXT_CHARS = 120_000


def _recent_chat_context() -> str:
    chats = []
    for chat in list_chat_history()[:_MAX_DREAM_CHATS]:
        messages = load_chat_session(chat.id)[-_MAX_MESSAGES_PER_CHAT:]
        chats.append(
            {
                "chat": chat.model_dump(mode="json"),
                "messages": [
                    message.model_dump(mode="json")
                    for message in messages
                ],
            }
        )
    context = json.dumps(chats, ensure_ascii=False, default=str)
    if len(context) > _MAX_DREAM_CONTEXT_CHARS:
        return (
            context[:_MAX_DREAM_CONTEXT_CHARS]
            + "\n[chat history truncated by the host]"
        )
    return context


def dream():
    print("Dreaming...\n")
    dream_agent = create_agent()
    prompt = (
        f"{DREAM_PROMPT}\n\n"
        "<chat_history>\n"
        f"{_recent_chat_context()}\n"
        "</chat_history>"
    )
    response = dream_agent.run(prompt, stream=True, stream_events=True)
    for event in response:
        if event.event == "RunContent":
            if event.content is not None:
                print(event.content, end="", flush=True)
    print("\n\nDone dreaming.")
