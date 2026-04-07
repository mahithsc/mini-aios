import os
import argparse
from pathlib import Path
from uuid import uuid4

from agno.agent import Agent
# from agno.models.openrouter import OpenRouter
from agno.models.ollama import Ollama
from dotenv import load_dotenv

load_dotenv()

UIS_DIR = Path(__file__).resolve().parent / "uis"
OLLAMA_HOST = os.getenv("AIOS_OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("AIOS_OLLAMA_MODEL", "gemma4:e4b")

agent = Agent(
    # system_message="You are a generative UI agent. Generate a complete index.html document using HTML, Tailwind CSS via CDN, and JavaScript when needed. Return only raw HTML for the file contents. Do not wrap the response in markdown code fences. Give me an entire index.html file. What you output should be a complete index.html file. It should be a valid html file with tailwind and js.",
    system_message="You are a helpful assistant.",
    # model=OpenRouter(id="openai/gpt-4o-mini"),
    model=Ollama(id=OLLAMA_MODEL, host=OLLAMA_HOST),
    markdown=True,
)


# agent.print_response("Make a Polymarket visualization UI to show the different orders")
response = agent.print_response("Make a Polymarket visualization UI to show the different orders")


# def create_visualization_scaffold(base_dir: Path = UIS_DIR) -> Path:
#     base_dir.mkdir(parents=True, exist_ok=True)
#     target_dir = base_dir / str(uuid4())
#     target_dir.mkdir()
#     return target_dir / "index.html"


# def strip_code_fences(content: str) -> str:
#     cleaned = content.strip()

#     if cleaned.startswith("```"):
#         lines = cleaned.splitlines()
#         if lines:
#             lines = lines[1:]
#         if lines and lines[-1].strip() == "```":
#             lines = lines[:-1]
#         cleaned = "\n".join(lines).strip()

#     return cleaned


# def generate_html(prompt: str) -> str:
#     run_output = agent.run(prompt, stream=False)
#     content = run_output.content
#     if not isinstance(content, str) or not content.strip():
#         raise ValueError("Agent returned empty content.")
#     return strip_code_fences(content)


# def parse_args() -> argparse.Namespace:
#     parser = argparse.ArgumentParser(description="Generate a UI into a new uis/<uuid>/index.html file.")
#     parser.add_argument("prompt", help="Prompt to send to the generative UI agent.")
#     return parser.parse_args()


# if __name__ == "__main__":
#     created_index_path = create_visualization_scaffold()
#     args = parse_args()
#     html = generate_html(args.prompt)
#     # created_index_path.write_text(html, encoding="utf-8")
#     print(f"Created UI folder UUID: {created_index_path.parent.name}")
#     print(f"Created visualization scaffold at: {created_index_path}")
#     print(html)
