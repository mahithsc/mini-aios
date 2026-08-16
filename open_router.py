"""Small local-model smoke test using the OpenAI-compatible Ollama API."""

import os

from agents import Agent, OpenAIChatCompletionsModel, RunConfig, Runner
from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()

OLLAMA_HOST = os.getenv("AIOS_OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("AIOS_OLLAMA_MODEL", "gemma4:e4b")


def main() -> None:
    client = AsyncOpenAI(base_url=f"{OLLAMA_HOST.rstrip('/')}/v1", api_key="ollama")
    agent = Agent(
        name="Local assistant",
        instructions="You are a helpful assistant.",
        model=OpenAIChatCompletionsModel(model=OLLAMA_MODEL, openai_client=client),
    )
    response = Runner.run_sync(
        agent,
        "Make a Polymarket visualization UI to show the different orders",
        run_config=RunConfig(tracing_disabled=True),
    )
    print(response.final_output or "")


if __name__ == "__main__":
    main()
