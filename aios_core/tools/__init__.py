__all__ = [
    "read",
    "write",
    "edit",
    "glob",
    "grep",
    "processes",
    "process_spawn",
    "process_list",
    "process_send",
    "process_poll",
    "process_kill",
    "bash",
    "cron",
    "assistant",
    "notify",
    "codex",
    "tavily_search",
    "fetch",
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
    if name == "assistant":
        from .assistant import assistant

        return assistant
    if name == "notify":
        from .notify import notify

        return notify
    if name == "codex":
        from .codex import codex

        return codex
    if name == "tavily_search":
        from .tavily import tavily_search

        return tavily_search
    if name == "fetch":
        from .fetch import fetch

        return fetch
    if name == "subagent":
        from .subagent import subagent

        return subagent
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
