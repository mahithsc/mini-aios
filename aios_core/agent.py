import os

from agno.agent import Agent
from agno.models.openai import OpenAIResponses
from dotenv import load_dotenv

from .assistants import load_assistant_context
from .agent_prompt import build_agent_prompt
from .skills import load_skills
from .sessions import (
    get_chat_artifacts_dir,
    get_chat_files_dir,
    get_chat_session_relative_dir,
)
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
from .tools.assistant import assistant
from .tools.canvas import show_canvas
from .tools.codex import codex
from .tools.cron import cron
from .tools.fetch import fetch
from .tools.notify import notify
from .tools.subagent import subagent
from .workspace import resolve_workspace_path

load_dotenv()

DEFAULT_CRON_TIMEZONE = os.getenv("AIOS_DEFAULT_TIMEZONE", "America/New_York")
DEFAULT_SERVER_BASE_URL = os.getenv("AIOS_SERVER_BASE_URL", "http://localhost:8765")


BASE_TOOLS = [
    assistant,
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
    fetch,
]
MAIN_TOOLS = [*BASE_TOOLS, subagent]


def _build_prompt(
    include_subagent_tool: bool = True,
    chat_id: str | None = None,
):
    current_chat_files_dir = None
    current_chat_artifacts_dir = None
    current_chat_artifact_url_template = None
    assistant_context = None
    if chat_id:
        current_chat_files_dir = str(get_chat_files_dir(chat_id))
        current_chat_artifacts_dir = str(get_chat_artifacts_dir(chat_id))
        sanitized_chat_id = get_chat_session_relative_dir(chat_id).name
        current_chat_artifact_url_template = (
            f"{DEFAULT_SERVER_BASE_URL}/session-artifacts/"
            f"{sanitized_chat_id}/<artifact-id>/index.html"
        )
        assistant_context = load_assistant_context(chat_id)

    return build_agent_prompt(
        include_subagent_tool=include_subagent_tool,
        default_cron_timezone=DEFAULT_CRON_TIMEZONE,
        workspace_dir=str(resolve_workspace_path(".")),
        current_chat_id=chat_id,
        current_chat_files_dir=current_chat_files_dir,
        current_chat_artifacts_dir=current_chat_artifacts_dir,
        current_chat_artifact_url_template=current_chat_artifact_url_template,
        assistant_title=assistant_context.assistant.title if assistant_context else None,
        assistant_identity=assistant_context.identity if assistant_context else None,
        assistant_memory=assistant_context.memory if assistant_context else None,
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
        model=OpenAIResponses(id="gpt-5.4", reasoning_effort="medium"),
    )

def create_main_agent(chat_id: str | None = None):
    return _create_agent_with_tools(
        MAIN_TOOLS,
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
