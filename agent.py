import json
import os

from tools import *
from agno.agent import Agent
from agno.models.openai import OpenAIChat
from dotenv import load_dotenv

load_dotenv()

SKILLS_INDEX_PATH = "skills/skills_index.json"
DEFAULT_MODEL_ID = os.getenv("AIOS_MODEL_ID", "gpt-4.1")

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

"cron": (
    "Manage scheduled cron jobs (actions: create, list, edit, delete)",
    {"action": "string", "name": "string?", "description": "string?",
     "instructions": "string?", "schedule": "string? (cron expression, e.g. '*/5 * * * *')",
     "cron_id": "string? (full cron UUID from create/list output)"},
    cron,
),
</tools>
"""


def _load_skills():
    try:
        with open(SKILLS_INDEX_PATH) as f:
            raw_skills = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []

    if isinstance(raw_skills, dict):
        raw_skills = raw_skills.get("skills", [])
    if not isinstance(raw_skills, list):
        return []
    return [skill for skill in raw_skills if isinstance(skill, dict)]


def _build_prompt():
    prompt = BASE_PROMPT
    skills = _load_skills()

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
        tools=[read, write, edit, glob, grep, bash, cron],
        model=OpenAIChat(id=DEFAULT_MODEL_ID),
    )
