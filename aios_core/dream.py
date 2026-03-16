from .agent import create_agent
from .prompt_loader import load_prompt

DREAM_PROMPT = load_prompt("dream.md")

def dream():
    print("Dreaming...\n")
    dream_agent = create_agent()
    response = dream_agent.run(DREAM_PROMPT, stream=True, stream_events=True)
    for event in response:
        if event.event == "RunContent":
            if event.content is not None:
                print(event.content, end="", flush=True)
    print("\n\nDone dreaming.")