from .agent.prompts import load_prompt
from .agent.runtime import run_agent_to_completion

DREAM_PROMPT = load_prompt("dream.md")


def dream():
    print("Dreaming...\n")
    output = run_agent_to_completion(DREAM_PROMPT)
    if output:
        print(output, end="", flush=True)
    print("\n\nDone dreaming.")
