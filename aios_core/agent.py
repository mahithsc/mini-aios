import os

from agno.agent import Agent
from agno.models.openai import OpenAIChat
from dotenv import load_dotenv

from .agent_prompt import build_agent_prompt
from .deploy.agent_tools import (
    activate_app_route,
    app_create,
    app_deployment_status,
    app_info,
    app_logs,
    app_restart,
    app_route_status,
    app_status,
    app_stop,
    app_workspace,
    apps_list,
    create_app_artifact,
    deploy_app_artifact,
    deployment_events,
    deployment_pipeline_status,
    deployment_status,
    prepare_app_route,
    rollback_app_artifact,
    secrets_list,
)
from .memory import build_memory_prompt
from .sessions import (
    get_chat_artifacts_dir,
    get_chat_files_dir,
    get_chat_session_relative_dir,
)
from .skills import load_skills
from .tools import (
    bash,
    edit,
    find_app_references,
    find_relevant_apps,
    glob,
    grep,
    inspect_app,
    list_app_files,
    process_kill,
    process_list,
    process_poll,
    process_send,
    process_spawn,
    read,
    read_app_file,
    search_app_content,
    tavily_search,
    write,
)
from .tools.canvas import show_canvas
from .tools.codex import codex
from .tools.codex_job import codex_answer, codex_poll, codex_start, codex_stop
from .tools.codex_subagent import codex_subagent
from .tools.cron import cron
from .tools.fetch import fetch
from .tools.generative_widget import generative_widget
from .tools.memory import memory
from .tools.notify import notify
from .tools.session_search import session_search
from .tools.subagent import subagent
from .workspace import resolve_workspace_path

load_dotenv()

DEFAULT_CRON_TIMEZONE = os.getenv("AIOS_DEFAULT_TIMEZONE", "America/New_York")
DEFAULT_SERVER_BASE_URL = os.getenv("AIOS_SERVER_BASE_URL", "http://localhost:8765")
DEFAULT_MODEL_ID = os.getenv("AIOS_MODEL_ID", "gpt-4.1")


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
    codex_subagent,
    codex_start,
    codex_poll,
    codex_answer,
    codex_stop,
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
    create_app_artifact,
    prepare_app_route,
    deploy_app_artifact,
    app_deployment_status,
    deployment_pipeline_status,
    deployment_status,
    deployment_events,
    activate_app_route,
    app_route_status,
    rollback_app_artifact,
    app_status,
    app_logs,
    app_restart,
    app_stop,
    find_relevant_apps,
    inspect_app,
    list_app_files,
    search_app_content,
    find_app_references,
    read_app_file,
    memory,
    session_search,
    subagent,
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
        system_message=_build_prompt(
            include_subagent_tool=include_subagent_tool,
            chat_id=chat_id,
        ),
        tools=tools,
        model=OpenAIChat(id=DEFAULT_MODEL_ID),
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
