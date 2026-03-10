import json

from tools import *
from agno.agent import Agent
from agno.models.openai.responses import OpenAIResponses
from dotenv import load_dotenv

load_dotenv()

SKILLS_INDEX_PATH = "skills/skills_index.json"

BASE_PROMPT = """\
You are a helpful coding agent
When using back commands, use the non interactive mode
Have bias for action, use your tools to get things done

Keep timeout for bash commands in 20 seconds.

<tools>
"read": (
    "Read file with line numbers (file path, not directory)",
    {"path": "string", "offset": "number?", "limit": "number?"},
    read,
),

"write": (
    "Write content to file",
    {"path": "string", "content": "string"},
    write,
),

"edit": (
    "Replace old with new in file (old must be unique unless all=true)",
    {"path": "string", "old": "string", "new": "string", "all": "boolean?"},
    edit,
),

"glob": (
    "Find files by pattern, sorted by mtime",
    {"pat": "string", "path": "string?"},
    glob,
),

"grep": (
    "Search files for regex pattern",
    {"pat": "string", "path": "string?"},
    grep,
),

"bash": (
    "Run shell command",
    {"cmd": "string", "timeout": "number?"},
    bash,
),
</tools>
"""


def _build_prompt():
    prompt = BASE_PROMPT
    try:
        with open(SKILLS_INDEX_PATH) as f:
            skills = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        skills = []

    if skills:
        prompt += "\n<skills>\n"
        prompt += "You have learned the following skills from past experience. "
        prompt += "Read the skill file before using it.\n\n"
        for skill in skills:
            prompt += f'- {skill["title"]}: {skill["summary"]} (file: {skill["file"]})\n'
        prompt += "</skills>\n"

    return prompt


def create_agent():
    return Agent(
        system_message=_build_prompt(),
        tools=[read, write, edit, glob, grep, bash],
        model=OpenAIResponses("gpt-5.4", reasoning_effort="medium"),
    )