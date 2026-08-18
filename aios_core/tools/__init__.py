__all__ = [
    "read",
    "write",
    "edit",
    "glob",
    "grep",
    "find_relevant_apps",
    "find_app_references",
    "inspect_app",
    "list_app_files",
    "read_app_file",
    "search_app",
    "search_app_content",
    "processes",
    "process_spawn",
    "process_list",
    "process_send",
    "process_poll",
    "process_kill",
    "bash",
    "cron",
    "notify",
    "codex",
    "codex_subagent",
    "codex_start",
    "codex_poll",
    "codex_stop",
    "apps_list",
    "app_status",
    "app_logs",
    "app_restart",
    "app_stop",
    "generative_widget",
    "tavily_search",
    "fetch",
    "memory",
    "session_search",
    "subagent",
]


def __getattr__(name: str):
    if name in {"read", "write", "edit"}:
        from .filesystem import edit, read, write

        return {"read": read, "write": write, "edit": edit}[name]
    if name in {"glob", "grep"}:
        from .search import glob, grep

        return {"glob": glob, "grep": grep}[name]
    if name in {
        "find_relevant_apps",
        "find_app_references",
        "inspect_app",
        "list_app_files",
        "read_app_file",
        "search_app",
        "search_app_content",
    }:
        from .app_search import (
            find_app_references,
            find_relevant_apps,
            inspect_app,
            list_app_files,
            read_app_file,
            search_app,
            search_app_content,
        )

        return {
            "find_relevant_apps": find_relevant_apps,
            "find_app_references": find_app_references,
            "inspect_app": inspect_app,
            "list_app_files": list_app_files,
            "read_app_file": read_app_file,
            "search_app": search_app,
            "search_app_content": search_app_content,
        }[name]
    if name in {
        "processes",
        "process_spawn",
        "process_list",
        "process_send",
        "process_poll",
        "process_kill",
    }:
        from .processes import (
            process_kill,
            process_list,
            process_poll,
            process_send,
            process_spawn,
            processes,
        )

        return {
            "processes": processes,
            "process_spawn": process_spawn,
            "process_list": process_list,
            "process_send": process_send,
            "process_poll": process_poll,
            "process_kill": process_kill,
        }[name]
    if name == "bash":
        from .shell import bash

        return bash
    if name == "cron":
        from .cron import cron

        return cron
    if name == "notify":
        from .notify import notify

        return notify
    if name == "codex":
        from .codex import codex

        return codex
    if name == "codex_subagent":
        from .codex_subagent import codex_subagent

        return codex_subagent
    if name in {"codex_start", "codex_poll", "codex_stop"}:
        from .codex_job import codex_poll, codex_start, codex_stop

        return {"codex_start": codex_start, "codex_poll": codex_poll, "codex_stop": codex_stop}[name]
    if name in {"apps_list", "app_status", "app_logs", "app_restart", "app_stop"}:
        from ..deploy.agent_tools import app_logs, app_restart, app_status, app_stop, apps_list

        return {
            "apps_list": apps_list,
            "app_status": app_status,
            "app_logs": app_logs,
            "app_restart": app_restart,
            "app_stop": app_stop,
        }[name]
    if name == "generative_widget":
        from .generative_widget import generative_widget

        return generative_widget
    if name == "tavily_search":
        from .tavily import tavily_search

        return tavily_search
    if name == "fetch":
        from .fetch import fetch

        return fetch
    if name == "memory":
        from .memory import memory

        return memory
    if name == "session_search":
        from .session_search import session_search

        return session_search
    if name == "subagent":
        from .subagent import subagent

        return subagent
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
