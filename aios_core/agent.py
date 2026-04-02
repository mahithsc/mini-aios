import json
import os
from pathlib import Path

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
from .tools.app import app
from .tools.codex import codex
from .tools.cron import cron
from .tools.fetch import fetch
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
    app,
    show_canvas,
    notify,
    tavily_search,
    fetch,
]
MAIN_TOOLS = [*BASE_TOOLS, subagent]


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

    skills = []
    for entry in raw_skills:
        if isinstance(entry, str):
            name = entry.strip()
            if not name:
                continue
            skills.append(
                {
                    "name": name,
                    "title": name,
                    "file": f"skills/{name}.md",
                }
            )
            continue

        if not isinstance(entry, dict):
            continue

        file_path = str(entry.get("file") or entry.get("path") or "").strip()
        name = str(
            entry.get("name")
            or entry.get("id")
            or entry.get("title")
            or Path(file_path).stem
        ).strip()
        if not name:
            continue

        skills.append(
            {
                "name": name,
                "title": str(entry.get("title") or name).strip() or name,
                "summary": str(
                    entry.get("summary") or entry.get("description") or ""
                ).strip(),
                "file": file_path or f"skills/{name}.md",
            }
        )

    return skills


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