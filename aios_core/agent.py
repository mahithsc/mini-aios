import json
import os

from agno.agent import Agent
from agno.models.openai import OpenAIChat
from dotenv import load_dotenv

from .agent_prompt import build_agent_prompt
from .tools import (
    bash,
    edit,
    glob,
    grep,
    process_kill,
    process_list,
    process_poll,
    process_send,
    process_spawn,
    read,
    tavily_search,
    write,
)
from .tools.canvas import show_canvas
from .tools.codex import codex
from .tools.cron import cron
from .tools.notify import notify
from .tools.subagent import subagent
from .workspace import resolve_workspace_path

load_dotenv()

SKILLS_INDEX_PATH = str(resolve_workspace_path("skills/skills_index.json"))
DEFAULT_CRON_TIMEZONE = os.getenv("AIOS_DEFAULT_TIMEZONE", "America/New_York")


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
    show_canvas,
    notify,
    tavily_search,
]
MAIN_TOOLS = [*BASE_TOOLS, subagent]


def _load_skills():
    try:
        with open(SKILLS_INDEX_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _build_prompt(include_subagent_tool: bool = True):
    return build_agent_prompt(
        include_subagent_tool=include_subagent_tool,
        default_cron_timezone=DEFAULT_CRON_TIMEZONE,
        workspace_dir=str(resolve_workspace_path(".")),
        skills=_load_skills(),
    )


def _create_agent_with_tools(tools, include_subagent_tool: bool):
    return Agent(
        system_message=_build_prompt(include_subagent_tool=include_subagent_tool),
        tools=tools,
        model=OpenAIChat(id="gpt-5.4"),
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