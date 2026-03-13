import json
from datetime import datetime
from zoneinfo import ZoneInfo

from .tools import *
from .workspace import resolve_workspace_path
from agno.agent import Agent
from agno.models.anthropic import Claude
from agno.models.openai import OpenAIChat
from dotenv import load_dotenv

load_dotenv()

SKILLS_INDEX_PATH = str(resolve_workspace_path("skills/skills_index.json"))

BASE_PROMPT_PREFIX = """\
You are a helpful coding agent
When using back commands, use the non interactive mode
Have bias for action, use your tools to get things done
For any delayed, recurring, or scheduled task, always use the cron tool.
Do not use bash backgrounding/scheduling patterns such as nohup, at, crontab, disown, sleep+&, or trailing &.

You should focus on executing tasks, not giving instructions on what to do.

Keep timeout for bash commands in 20 seconds.
"""

TOOLS_COMMON_DOC = """\
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

"codex": (
    "Delegate one coding task to Codex CLI (codex exec). "
    "Use for complex edits where a separate coding agent may perform better.",
    {"task": "string", "timeout": "number?", "model": "string?",
     "sandbox": "string? (read-only|workspace-write|danger-full-access)",
     "path": "string? (working directory; default '.')"},
    codex,
),

"cron": (
    "Manage scheduled cron jobs (actions: create, list, edit, delete)",
    {"action": "string", "name": "string?", "description": "string?",
     "instructions": "string?", "schedule": "string? (cron expression, e.g. '*/5 * * * *')",
     "cron_id": "string? (first 8 chars suffice)"},
    cron,
),

"tavily_search": (
    "Search the web with Tavily using TAVILY_API_KEY",
    {"query": "string", "search_depth": "string?", "max_results": "number?",
     "topic": "string?", "include_answer": "boolean?", "include_raw_content": "boolean?",
     "include_domains": "array?", "exclude_domains": "array?", "time_range": "string?",
     "timeout": "number?"},
    tavily_search,
),
"""

TOOLS_SUBAGENT_DOC = """\
"subagent": (
    "Delegate one focused task to a synchronous subagent. "
    "For parallel work, call this tool multiple times.",
    {"task": "string", "timeout": "number?"},
    subagent,
),
"""


BASE_TOOLS = [read, write, edit, glob, grep, bash, codex, cron, tavily_search]
MAIN_TOOLS = [*BASE_TOOLS, subagent]


def _build_prompt(include_subagent_tool: bool = True):
    prompt = BASE_PROMPT_PREFIX
    prompt += "\n<tools>\n"
    prompt += TOOLS_COMMON_DOC
    if include_subagent_tool:
        prompt += "\n" + TOOLS_SUBAGENT_DOC
    prompt += "</tools>\n"

    est_now = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S %Z")
    prompt += f"\nCurrent EST time: {est_now}\n"
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