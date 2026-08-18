from __future__ import annotations

import importlib

project_tool = importlib.import_module("aios_core.agent.tools.project")


def test_project_tool_routes_every_action(monkeypatch) -> None:
    calls: list[tuple] = []
    monkeypatch.setattr(
        project_tool,
        "create_project",
        lambda name: calls.append(("create", name)) or {"action": "create"},
    )
    monkeypatch.setattr(
        project_tool,
        "get_project",
        lambda project_id: calls.append(("get", project_id)) or {"action": "get"},
    )
    monkeypatch.setattr(
        project_tool,
        "list_projects",
        lambda: calls.append(("list",)) or {"action": "list"},
    )
    monkeypatch.setattr(
        project_tool,
        "update_project",
        lambda project_id, name: calls.append(("update", project_id, name))
        or {"action": "update"},
    )
    monkeypatch.setattr(
        project_tool,
        "delete_project",
        lambda project_id: calls.append(("delete", project_id))
        or {"action": "delete"},
    )

    assert project_tool.project("create", name="Example") == {"action": "create"}
    assert project_tool.project("get", project_id="proj_1") == {"action": "get"}
    assert project_tool.project("list") == {"action": "list"}
    assert project_tool.project(
        "update",
        project_id="proj_1",
        name="Renamed",
    ) == {"action": "update"}
    assert project_tool.project("delete", project_id="proj_1") == {
        "action": "delete"
    }
    assert calls == [
        ("create", "Example"),
        ("get", "proj_1"),
        ("list",),
        ("update", "proj_1", "Renamed"),
        ("delete", "proj_1"),
    ]


def test_project_tool_reports_invalid_actions_and_domain_errors(monkeypatch) -> None:
    from aios_core.projects import ProjectError

    assert project_tool.project("unknown")["success"] is False
    monkeypatch.setattr(
        project_tool,
        "create_project",
        lambda _name: (_ for _ in ()).throw(ProjectError("bad project")),
    )
    assert project_tool.project("create", name="Example") == {
        "success": False,
        "error": "bad project",
    }
