from __future__ import annotations

import json
import os
from pathlib import Path

from aios_core import workspace


def test_data_root_defaults_and_typed_helpers(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository = tmp_path / "mini-aios"
    repository.mkdir()
    monkeypatch.setattr(workspace, "_PROJECT_ROOT", repository)
    monkeypatch.delenv("AIOS_DATA_DIR", raising=False)
    monkeypatch.setenv("AIOS_ENV", "dev")

    assert workspace.get_data_dir() == repository / ".mini-aios"
    assert workspace.get_state_dir() == repository / ".mini-aios" / "state"
    assert workspace.get_projects_dir() == repository / ".mini-aios" / "projects"
    assert workspace.get_sessions_dir() == repository / ".mini-aios" / "sessions"
    assert workspace.get_uploads_dir() == repository / ".mini-aios" / "uploads"
    assert workspace.get_artifacts_dir() == repository / ".mini-aios" / "artifacts"
    assert workspace.get_runs_dir() == repository / ".mini-aios" / "runs"
    assert workspace.get_skills_dir() == repository / ".mini-aios" / "skills"
    assert workspace.get_memories_dir() == repository / ".mini-aios" / "memories"
    assert workspace.get_deployments_dir() == repository / ".mini-aios" / "deployments"
    assert workspace.get_cron_logs_dir() == repository / ".mini-aios" / "cron_logs"

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AIOS_ENV", "production")
    assert workspace.get_data_dir() == tmp_path / "home" / ".mini-aios"


def test_explicit_data_root_is_isolated_from_checkout(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository = tmp_path / "mini-aios"
    legacy_workspace = repository / "workspace"
    legacy_workspace.mkdir(parents=True)
    (legacy_workspace / "do-not-move.txt").write_text("live", encoding="utf-8")
    configured = tmp_path / "isolated-data"

    monkeypatch.setattr(workspace, "_PROJECT_ROOT", repository)
    monkeypatch.setenv("AIOS_DATA_DIR", str(configured))

    assert workspace.ensure_data_dir() == configured
    assert (legacy_workspace / "do-not-move.txt").read_text(encoding="utf-8") == "live"
    assert not (configured / "state" / "migrations").exists()
    assert all(path.is_dir() for path in workspace._layout_directories(configured))


def test_storage_migration_promotes_active_db_and_preserves_collisions(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "mini-aios"
    data_dir = repository / ".mini-aios"
    old_workspace = repository / "workspace"
    old_state = repository / "state"

    (old_workspace / "session" / "chat-1" / "files").mkdir(parents=True)
    (old_workspace / "session" / "chat-1" / "uploads").mkdir()
    (old_workspace / "session" / "chat-1" / "artifacts").mkdir()
    (old_workspace / "apps" / "app-1").mkdir(parents=True)
    (old_workspace / "deploy").mkdir()
    (old_workspace / "skills").mkdir()
    (old_workspace / "runs").mkdir()
    (old_workspace / "applications" / "abandoned").mkdir(parents=True)
    (old_state / "runs").mkdir(parents=True)
    (repository / "memories").mkdir(parents=True)
    (data_dir / "runs").mkdir(parents=True)

    (old_workspace / "aios.db").write_bytes(b"active-workspace-db")
    (old_workspace / "aios.db-wal").write_bytes(b"active-wal")
    (old_state / "aios.db").write_bytes(b"older-state-db")
    (old_state / "aios.db-wal").write_bytes(b"older-state-wal")
    (old_state / "credentials.key").write_text("secret", encoding="utf-8")
    (old_workspace / "session" / "session_manifest.json").write_text(
        "[]", encoding="utf-8"
    )
    (old_workspace / "session" / "chat-1" / "files" / "draft.txt").write_text(
        "draft", encoding="utf-8"
    )
    (old_workspace / "session" / "chat-1" / "uploads" / "source.pdf").write_text(
        "upload", encoding="utf-8"
    )
    (old_workspace / "session" / "chat-1" / "artifacts" / "result.txt").write_text(
        "artifact", encoding="utf-8"
    )
    (old_workspace / "apps" / "app-1" / "app.py").write_text("app", encoding="utf-8")
    (old_workspace / "deploy" / "projects.json").write_text("{}", encoding="utf-8")
    (old_workspace / "skills" / "index.json").write_text("{}", encoding="utf-8")
    (old_workspace / "runs" / "same.json").write_text("workspace", encoding="utf-8")
    (old_state / "runs" / "same.json").write_text("state", encoding="utf-8")
    (data_dir / "runs" / "same.json").write_text("canonical", encoding="utf-8")
    (repository / "memories" / "MEMORY.md").write_text("remember", encoding="utf-8")

    report = workspace.migrate_storage_layout(
        data_dir=data_dir,
        project_root=repository,
        production=False,
    )

    assert report["version"] == 1
    assert report["status"] == "complete"
    assert report["legacyDatabaseDisposition"]["archiveOnly"] == [
        "gateway_events",
        "unrecognized tables",
    ]
    assert (data_dir / "state" / "aios.db").read_bytes() == b"active-workspace-db"
    assert (data_dir / "state" / "aios.db-wal").read_bytes() == b"active-wal"
    assert (data_dir / "state" / "credentials.key").read_text(encoding="utf-8") == "secret"
    assert (data_dir / "sessions" / "session_manifest.json").is_file()
    assert (data_dir / "sessions" / "chat-1" / "scratch" / "draft.txt").is_file()
    assert (data_dir / "uploads" / "chat-1" / "source.pdf").is_file()
    assert (data_dir / "artifacts" / "chat-1" / "result.txt").is_file()
    assert (data_dir / "projects" / "app-1" / "app.py").is_file()
    assert (data_dir / "deployments" / "projects.json").is_file()
    assert (data_dir / "skills" / "index.json").is_file()
    assert (data_dir / "memories" / "MEMORY.md").is_file()
    assert (data_dir / "runs" / "same.json").read_text(encoding="utf-8") == "canonical"

    archive = data_dir / "legacy" / "storage-layout-v1"
    assert (archive / "state" / "aios.db").read_bytes() == b"older-state-db"
    assert (archive / "workspace" / "applications" / "abandoned").is_dir()
    archived_run_contents = {
        path.read_text(encoding="utf-8")
        for path in archive.rglob("same.json*")
        if path.is_file()
    }
    assert archived_run_contents == {"workspace", "state"}

    report_path = data_dir / "state" / "migrations" / "storage-layout-v1.json"
    assert json.loads(report_path.read_text(encoding="utf-8")) == report
    assert workspace.migrate_storage_layout(
        data_dir=data_dir,
        project_root=repository,
        production=False,
    ) == report


def test_production_migration_archives_preexisting_state_database(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / ".mini-aios"
    (data_dir / "state").mkdir(parents=True)
    (data_dir / "workspace").mkdir()
    (data_dir / "state" / "aios.db").write_bytes(b"older-state-db")
    (data_dir / "state" / "credentials.key").write_text("secret", encoding="utf-8")
    (data_dir / "workspace" / "aios.db").write_bytes(b"active-workspace-db")

    workspace.migrate_storage_layout(
        data_dir=data_dir,
        project_root=tmp_path / "unused-repository",
        production=True,
    )

    assert (data_dir / "state" / "aios.db").read_bytes() == b"active-workspace-db"
    assert (data_dir / "state" / "credentials.key").read_text(encoding="utf-8") == "secret"
    assert (
        data_dir / "legacy" / "storage-layout-v1" / "state" / "aios.db"
    ).read_bytes() == b"older-state-db"


def test_compatibility_path_resolution_translates_legacy_prefixes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    data_dir = tmp_path / ".mini-aios"
    monkeypatch.setenv("AIOS_DATA_DIR", str(data_dir))

    assert workspace.resolve_workspace_path("session/chat-1") == (
        data_dir / "sessions" / "chat-1"
    )
    assert workspace.resolve_workspace_path("apps/app-1") == (
        data_dir / "projects" / "app-1"
    )
    assert workspace.resolve_workspace_path("deploy/projects.json") == (
        data_dir / "deployments" / "projects.json"
    )
    assert workspace.resolve_workspace_path("aios.db") == data_dir / "state" / "aios.db"


def test_runtime_start_does_not_change_process_working_directory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from aios_core import initialize

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(initialize, "initialize_files", lambda: None)
    monkeypatch.setattr(initialize, "_RUNTIME_STARTED", False)

    initialize.start_runtime(start_crons=False)

    assert Path(os.getcwd()) == tmp_path
    monkeypatch.setattr(initialize, "_RUNTIME_STARTED", False)
