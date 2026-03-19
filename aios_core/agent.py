import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from .prompt_loader import load_prompt
from .workspace import resolve_workspace_path
from .tools import (
    bash,
    codex,
    cron,
    edit,
    glob,
    grep,
    process_kill,
    process_list,
    process_poll,
    process_send,
    process_spawn,
    read,
    subagent,
    tavily_search,
    write,
)
from agno.agent import Agent
from agno.models.anthropic import Claude
from agno.models.openai import OpenAIChat
from dotenv import load_dotenv

load_dotenv()

SKILLS_INDEX_PATH = str(resolve_workspace_path("skills/skills_index.json"))
DEFAULT_CRON_TIMEZONE = os.getenv("AIOS_DEFAULT_TIMEZONE", "America/New_York")
SUBAGENT_TOOLS = """
"subagent": (
    "Delegate one focused task to a synchronous subagent. "
    "For parallel work, call this tool multiple times.",
    {"task": "string", "timeout": "number?"},
    subagent,
),
"""


BASE_TOOLS = [
    read,
    write,
    edit,
    glob,
    grep,
    bash,
    process_spawn,
    process_list,
    process_send,
    process_poll,
    process_kill,
    codex,
    cron,
    tavily_search,
]
MAIN_TOOLS = [*BASE_TOOLS, subagent]


def _build_prompt(include_subagent_tool: bool = True):
    prompt = load_prompt("agent.md").replace(
        "$subagent_tools",
        SUBAGENT_TOOLS if include_subagent_tool else "",
    )

    scheduler_now = datetime.now(ZoneInfo(DEFAULT_CRON_TIMEZONE)).strftime("%Y-%m-%d %H:%M:%S %Z")
    utc_now = datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S %Z")
    prompt += (
        f"\nCurrent scheduler time ({DEFAULT_CRON_TIMEZONE}): {scheduler_now}\n"
        f"Current UTC time: {utc_now}\n"
        f"Default cron timezone: {DEFAULT_CRON_TIMEZONE}\n"
    )
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


def _create_agent_with_tools(tools, include_subagent_tool: bool):
    return Agent(
        system_message=_build_prompt(include_subagent_tool=include_subagent_tool),
        tools=tools,
        model=OpenAIChat(id="gpt-5.2", reasoning_effort="medium"),
    )

def create_main_agent():
    return _create_agent_with_tools(MAIN_TOOLS, include_subagent_tool=True)


def create_subagent_worker():
    return _create_agent_with_tools(BASE_TOOLS, include_subagent_tool=False)


def create_agent(include_subagent: bool = True):
    # Backward-compatible alias used across the codebase.
    if include_subagent:
        return create_main_agent()
    return create_subagent_worker()