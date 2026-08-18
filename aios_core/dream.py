from agents import RunConfig, Runner

from .agent import create_agent
from .agent.prompts import load_prompt

DREAM_PROMPT = load_prompt("dream.md")

def dream():
    print("Dreaming...\n")
    dream_agent = create_agent()
    response = Runner.run_sync(
        dream_agent,
        DREAM_PROMPT,
        max_turns=None,
        run_config=RunConfig(tracing_disabled=True),
    )
    if response.final_output is not None:
        print(response.final_output, end="", flush=True)
    print("\n\nDone dreaming.")
