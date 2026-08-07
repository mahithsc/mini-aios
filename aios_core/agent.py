import os

from agno.agent import Agent
from dotenv import load_dotenv

from .agent_prompt import build_agent_prompt
from .openai_chat import AiosOpenAIChat
from .skills import load_skills
from .tools import (
    app,
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
    read_skill,
    tavily_search,
    write,
)
from .tools.canvas import show_canvas
from .tools.codex import codex
from .tools.cron import cron
from .tools.fetch import fetch
from .tools.generative_widget import generative_widget
from .tools.notify import notify
from .tools.subagent import subagent
from .workspace import get_runtime_paths

load_dotenv()

DEFAULT_CRON_TIMEZONE = os.getenv("AIOS_DEFAULT_TIMEZONE", "America/New_York")
DEFAULT_MODEL_ID = os.getenv("AIOS_MODEL_ID", "gpt-4.1")


BASE_TOOLS = [
    read,
    write,
    edit,
    glob,
    grep,
    read_skill,
    bash,
    process_spawn,
    process_list,
    process_send,
    process_poll,
    process_kill,
    codex,
    cron,
    show_canvas,
    generative_widget,
    notify,
    tavily_search,
    fetch,
]
MAIN_TOOLS = [*BASE_TOOLS, app, subagent]


def _load_main_integration_tools() -> list:
    """Load connected integrations without making them startup requirements."""
    toolkits = []
    try:
        from .integrations.google_mcp import get_google_mcp_toolkits
    except ImportError as exc:
        print(f"[integrations] Google Workspace MCP unavailable: {exc}")
    else:
        try:
            toolkits.extend(get_google_mcp_toolkits())
        except Exception as exc:  # noqa: BLE001 - optional integration
            print(f"[integrations] Google Workspace MCP could not be loaded: {exc}")

    try:
        from .integrations.doordash_mcp import get_doordash_mcp_toolkit
    except ImportError as exc:
        print(f"[integrations] DoorDash MCP unavailable: {exc}")
    else:
        try:
            doordash = get_doordash_mcp_toolkit()
            if doordash is not None:
                toolkits.append(doordash)
        except Exception as exc:  # noqa: BLE001 - optional integration
            print(f"[integrations] DoorDash MCP could not be loaded: {exc}")

    try:
        from .apps.mcp import get_enabled_app_mcp_toolkits
    except ImportError as exc:
        print(f"[apps] App MCP support unavailable: {exc}")
    else:
        try:
            toolkits.extend(get_enabled_app_mcp_toolkits())
        except Exception as exc:  # noqa: BLE001 - optional App toolkits
            print(f"[apps] App MCP toolkits could not be loaded: {exc}")

    return toolkits


def _build_prompt(
    include_subagent_tool: bool = True,
    chat_id: str | None = None,
):
    paths = get_runtime_paths()
    return build_agent_prompt(
        include_subagent_tool=include_subagent_tool,
        default_cron_timezone=DEFAULT_CRON_TIMEZONE,
        workspace_dir=str(paths.workspace),
        applications_dir=str(paths.applications),
        uploads_dir=str(paths.uploads),
        downloads_dir=str(paths.downloads),
        skills_dir=str(paths.skills),
        current_chat_id=chat_id,
        skills=load_skills(),
    )


def _create_agent_with_tools(
    tools,
    include_subagent_tool: bool,
    chat_id: str | None = None,
):
    return Agent(
        system_message=_build_prompt(
            include_subagent_tool=include_subagent_tool,
            chat_id=chat_id,
        ),
        tools=tools,
        model=AiosOpenAIChat(id=DEFAULT_MODEL_ID),
    )

def create_main_agent(chat_id: str | None = None):
    return _create_agent_with_tools(
        [*MAIN_TOOLS, *_load_main_integration_tools()],
        include_subagent_tool=True,
        chat_id=chat_id,
    )


def create_subagent_worker(chat_id: str | None = None):
    return _create_agent_with_tools(
        BASE_TOOLS,
        include_subagent_tool=False,
        chat_id=chat_id,
    )


def create_agent(
    include_subagent: bool = True,
    *,
    chat_id: str | None = None,
):
    # Backward-compatible alias used across the codebase.
    if include_subagent:
        return create_main_agent(chat_id=chat_id)
    return create_subagent_worker(chat_id=chat_id)
