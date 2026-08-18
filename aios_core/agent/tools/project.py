"""Single model-facing router for durable project lifecycle operations."""

from __future__ import annotations

from typing import Literal

from ...projects import (
    ProjectError,
    create_project,
    delete_project,
    get_project,
    list_projects,
    update_project,
)

ProjectAction = Literal["create", "get", "list", "update", "delete"]


def project(
    action: ProjectAction,
    project_id: str | None = None,
    name: str | None = None,
) -> dict:
    """Create, inspect, list, update, or delete a durable project.

    ``create`` requires ``name``. ``get`` and ``delete`` require
    ``project_id``. ``update`` requires both. ``list`` takes no additional
    fields. A created project contains only ``project.md``; use ordinary file
    tools to choose the rest of its implementation. Delete removes the entire
    project directory and must only follow an explicit user request.
    """

    if not isinstance(action, str):
        return {"success": False, "error": "action is required"}
    normalized_action = action.strip().lower()
    try:
        if normalized_action == "create":
            return create_project(name or "")
        if normalized_action == "get":
            return get_project(project_id or "")
        if normalized_action == "list":
            return list_projects()
        if normalized_action == "update":
            return update_project(project_id or "", name or "")
        if normalized_action == "delete":
            return delete_project(project_id or "")
        return {
            "success": False,
            "error": "unknown action; use create, get, list, update, or delete",
        }
    except (ProjectError, OSError) as exc:
        return {"success": False, "error": str(exc)}


__all__ = ["project"]
