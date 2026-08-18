from __future__ import annotations

import json
from pathlib import Path

import pytest

from aios_core.app_workspaces import (
    AppWorkspaceError,
    create_app_workspace,
    find_legacy_app_workspaces,
    list_app_workspaces,
    resolve_app_workspace,
)

APP_ID = "app_cloud123"


def _legacy_app(
    session_dir: Path,
    chat_id: str,
    relative: str,
    files: int,
    *,
    scratch_name: str = "files",
) -> Path:
    root = session_dir / chat_id / scratch_name / relative
    root.mkdir(parents=True)
    (root / "aios.deploy.yaml").write_text(
        f"version: 1\napp_id: {APP_ID}\nfrontend:\n  source: frontend\n",
        encoding="utf-8",
    )
    frontend = root / "frontend"
    frontend.mkdir()
    for index in range(files):
        (frontend / f"source_{index}.js").write_text(
            f"export const value{index} = {index};\n", encoding="utf-8"
        )
    return root


def test_create_app_workspace_adds_metadata_and_readmes(tmp_path: Path) -> None:
    apps_dir = tmp_path / "apps"

    result = create_app_workspace(
        APP_ID,
        "Example App",
        origin_chat_id="chat-1",
        apps_dir=apps_dir,
    )

    root = apps_dir / APP_ID
    assert result["workspace_path"] == str(root.resolve())
    assert result["created"] is True
    assert "Example App" in (root / "README.md").read_text(encoding="utf-8")
    assert "durable source-of-truth" in (apps_dir / "README.md").read_text(
        encoding="utf-8"
    )
    metadata = json.loads((root / ".aios-app.json").read_text(encoding="utf-8"))
    assert metadata["app_id"] == APP_ID
    assert metadata["origin_chat_id"] == "chat-1"


def test_default_app_root_is_the_projects_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projects_dir = tmp_path / ".mini-aios" / "projects"
    monkeypatch.setattr(
        "aios_core.app_workspaces.get_projects_dir",
        lambda: projects_dir,
    )

    result = create_app_workspace(APP_ID, "Example App")

    assert result["workspace_path"] == str((projects_dir / APP_ID).resolve())


def test_create_app_workspace_preserves_project_readme(tmp_path: Path) -> None:
    root = tmp_path / "apps" / APP_ID
    root.mkdir(parents=True)
    (root / "README.md").write_text("# Custom project docs\n", encoding="utf-8")

    create_app_workspace(APP_ID, "Example App", apps_dir=tmp_path / "apps")

    assert (root / "README.md").read_text(encoding="utf-8") == (
        "# Custom project docs\n"
    )


def test_app_workspace_rejects_symlinked_canonical_root(tmp_path: Path) -> None:
    apps_dir = tmp_path / "apps"
    apps_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (apps_dir / APP_ID).symlink_to(outside, target_is_directory=True)

    with pytest.raises(AppWorkspaceError, match="symbolic link"):
        create_app_workspace(APP_ID, "Example App", apps_dir=apps_dir)
    assert list_app_workspaces(apps_dir=apps_dir)["apps"] == []
    assert not (outside / ".aios-app.json").exists()


def test_list_app_workspaces_includes_complete_and_unfinished_apps(
    tmp_path: Path,
) -> None:
    apps_dir = tmp_path / "apps"
    complete = apps_dir / APP_ID
    complete.mkdir(parents=True)
    (complete / ".aios-app.json").write_text(
        json.dumps({"app_id": APP_ID, "name": "Complete App"}),
        encoding="utf-8",
    )
    (complete / "aios.deploy.yaml").write_text(
        f"version: 1\napp_id: {APP_ID}\ndatabase:\n  migrations: migrations\nserver:\n  source: server\n",
        encoding="utf-8",
    )
    (complete / "server").mkdir()
    (complete / "server" / "app.py").write_text("print('ready')\n", encoding="utf-8")

    unfinished = apps_dir / "app_unfinished"
    unfinished.mkdir()
    (unfinished / "README.md").write_text(
        "# Half Finished App\n\nStill being built.\n", encoding="utf-8"
    )

    result = list_app_workspaces(apps_dir=apps_dir)

    assert result["apps_dir"] == str(apps_dir.resolve())
    assert result["apps"] == [
        {
            "id": APP_ID,
            "app_id": APP_ID,
            "name": "Complete App",
            "workspace_path": str(complete.resolve()),
            "has_metadata": True,
            "has_manifest": True,
            "has_source": True,
            "components": ["database", "server"],
        },
        {
            "id": "app_unfinished",
            "app_id": "app_unfinished",
            "name": "Half Finished App",
            "workspace_path": str(unfinished.resolve()),
            "has_metadata": False,
            "has_manifest": False,
            "has_source": False,
            "components": [],
        },
    ]


def test_list_app_workspaces_uses_manifest_and_package_fallbacks(
    tmp_path: Path,
) -> None:
    apps_dir = tmp_path / "apps"
    manifest_only = apps_dir / "draft-backend"
    manifest_only.mkdir(parents=True)
    (manifest_only / "aios.deploy.yaml").write_text(
        "version: 1\napp_id: app_manifest123\nfrontend:\n  source: frontend\n",
        encoding="utf-8",
    )
    (manifest_only / "package.json").write_text(
        json.dumps({"name": "manifest-package"}), encoding="utf-8"
    )

    app = list_app_workspaces(apps_dir=apps_dir)["apps"][0]

    assert app["app_id"] == "app_manifest123"
    assert app["name"] == "manifest-package"
    assert app["components"] == ["frontend"]


def test_source_detection_handles_nested_and_manifest_only_workspaces(
    tmp_path: Path,
) -> None:
    apps_dir = tmp_path / "apps"
    nested = apps_dir / "app_nested"
    (nested / "frontend" / "src").mkdir(parents=True)
    (nested / "README.md").write_text("# Nested\n", encoding="utf-8")
    (nested / "frontend" / "src" / "app.ts").write_text(
        "export {};\n", encoding="utf-8"
    )
    manifest_only = apps_dir / "app_manifestonly"
    manifest_only.mkdir()
    (manifest_only / "aios.deploy.yaml").write_text(
        "version: 1\napp_id: app_manifestonly\nfrontend:\n  source: frontend\n",
        encoding="utf-8",
    )

    apps = {
        app["app_id"]: app for app in list_app_workspaces(apps_dir=apps_dir)["apps"]
    }

    assert apps["app_nested"]["has_source"] is True
    assert apps["app_manifestonly"]["has_source"] is False


def test_resolve_rejects_manifest_identity_mismatch(tmp_path: Path) -> None:
    root = tmp_path / "apps" / APP_ID
    root.mkdir(parents=True)
    (root / "aios.deploy.yaml").write_text(
        "version: 1\napp_id: app_other\nfrontend:\n  source: frontend\n",
        encoding="utf-8",
    )
    (root / "index.html").write_text("<h1>Wrong app</h1>\n", encoding="utf-8")

    with pytest.raises(AppWorkspaceError, match="different app"):
        resolve_app_workspace(APP_ID, apps_dir=tmp_path / "apps")


def test_resolve_app_workspace_adopts_richest_legacy_source(tmp_path: Path) -> None:
    sessions = tmp_path / "session"
    sparse = _legacy_app(sessions, "chat-new", "replacement", files=1)
    rich = _legacy_app(sessions, "chat-original", "real-project", files=5)

    candidates = find_legacy_app_workspaces(APP_ID, session_dir=sessions)
    result = resolve_app_workspace(
        APP_ID,
        name="Example App",
        apps_dir=tmp_path / "apps",
        session_dir=sessions,
    )

    assert candidates == [rich, sparse]
    assert result["found"] is True
    assert result["migrated_from"] == str(rich.resolve())
    adopted = Path(result["workspace_path"])
    assert (adopted / "frontend/source_4.js").is_file()
    metadata = json.loads((adopted / ".aios-app.json").read_text(encoding="utf-8"))
    assert metadata["origin_chat_id"] == "chat-original"


def test_resolve_app_workspace_adopts_source_from_canonical_chat_scratch(
    tmp_path: Path,
) -> None:
    sessions = tmp_path / "sessions"
    source = _legacy_app(
        sessions,
        "chat-original",
        "real-project",
        files=2,
        scratch_name="scratch",
    )

    candidates = find_legacy_app_workspaces(APP_ID, session_dir=sessions)
    result = resolve_app_workspace(
        APP_ID,
        name="Example App",
        apps_dir=tmp_path / "projects",
        session_dir=sessions,
    )

    assert candidates == [source]
    assert result["migrated_from"] == str(source.resolve())
    assert (Path(result["workspace_path"]) / "frontend/source_1.js").is_file()


def test_resolve_app_workspace_does_not_fabricate_missing_source(
    tmp_path: Path,
) -> None:
    result = resolve_app_workspace(
        APP_ID,
        apps_dir=tmp_path / "apps",
        session_dir=tmp_path / "session",
    )

    assert result["found"] is False
    assert "Do not fabricate" in result["error"]
    assert not (tmp_path / "apps" / APP_ID).exists()
