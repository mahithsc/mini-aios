import os
from collections.abc import Mapping

from agents import Agent, ModelSettings
from dotenv import load_dotenv
from openai.types.shared import Reasoning

from .tools.apps import (
    app_create,
    app_info,
    app_logs,
    app_restart,
    app_status,
    app_stop,
    app_workspace,
    apps_list,
    legacy_apps_list,
    secrets_list,
)
from ..memory import build_memory_prompt
from ..sessions import (
    get_chat_artifacts_dir,
    get_chat_files_dir,
    get_chat_session_relative_dir,
)
from ..skills import load_skills
from .tools import (
    bash,
    edit,
    glob,
    grep,
    read,
    tavily_search,
    write,
)
from ..workspace import resolve_workspace_path
from .openai import as_function_tool
from .pi.tool import pi
from .prompts.builder import build_agent_prompt
from .tools.canvas import show_canvas
from .tools.cron import cron
from .tools.fetch import fetch
from .tools.generative_widget import generative_widget
from .tools.memory import memory
from .tools.notify import notify
from .tools.session_search import session_search
from .tools.subagent import subagent

load_dotenv()

DEFAULT_CRON_TIMEZONE = os.getenv("AIOS_DEFAULT_TIMEZONE", "America/New_York")
DEFAULT_SERVER_BASE_URL = os.getenv("AIOS_SERVER_BASE_URL", "http://localhost:8765")


def _resolve_model_configuration(
    environment: Mapping[str, str],
) -> tuple[str, str | None]:
    model_id = environment.get("AIOS_MODEL_ID", "gpt-5.6")
    reasoning_effort = environment.get("AIOS_REASONING_EFFORT")
    if reasoning_effort is None and model_id.startswith("gpt-5.6"):
        reasoning_effort = "xhigh"
    return model_id, reasoning_effort


DEFAULT_MODEL_ID, DEFAULT_REASONING_EFFORT = _resolve_model_configuration(os.environ)


BASE_TOOLS = [
    read,
    write,
    edit,
    glob,
    grep,
    bash,
    pi,
    cron,
    show_canvas,
    generative_widget,
    notify,
    tavily_search,
    fetch,
]
MAIN_TOOLS = [
    *BASE_TOOLS,
    memory,
    session_search,
    subagent,
    app_create,
    app_workspace,
    app_info,
    secrets_list,
    apps_list,
    legacy_apps_list,
    app_status,
    app_logs,
    app_restart,
    app_stop,
]


def _build_prompt(
    include_subagent_tool: bool = True,
    chat_id: str | None = None,
):
    current_chat_files_dir = None
    current_chat_artifacts_dir = None
    current_chat_artifact_url_template = None
    if chat_id:
        current_chat_files_dir = str(get_chat_files_dir(chat_id))
        current_chat_artifacts_dir = str(get_chat_artifacts_dir(chat_id))
        sanitized_chat_id = get_chat_session_relative_dir(chat_id).name
        current_chat_artifact_url_template = (
            f"{DEFAULT_SERVER_BASE_URL}/session-artifacts/"
            f"{sanitized_chat_id}/<artifact-id>/index.html"
        )

    return build_agent_prompt(
        include_subagent_tool=include_subagent_tool,
        default_cron_timezone=DEFAULT_CRON_TIMEZONE,
        workspace_dir=str(resolve_workspace_path(".")),
        current_chat_id=chat_id,
        current_chat_files_dir=current_chat_files_dir,
        current_chat_artifacts_dir=current_chat_artifacts_dir,
        current_chat_artifact_url_template=current_chat_artifact_url_template,
        include_memory_tools=include_subagent_tool,
        memory_context=build_memory_prompt(),
        skills=load_skills(),
    )


def _create_agent_with_tools(
    tools,
    include_subagent_tool: bool,
    chat_id: str | None = None,
):
    return Agent(
        name="AIOS" if include_subagent_tool else "AIOS subagent",
        instructions=_build_prompt(
            include_subagent_tool=include_subagent_tool,
            chat_id=chat_id,
        ),
        tools=[as_function_tool(tool) for tool in tools],
        model=DEFAULT_MODEL_ID,
        model_settings=ModelSettings(
            reasoning=(
                Reasoning(effort=DEFAULT_REASONING_EFFORT)
                if DEFAULT_REASONING_EFFORT is not None
                else None
            ),
        ),
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
