from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from aios_core import workspace


def _create_sqlite_database(path: Path, marker: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
        connection.execute("INSERT INTO marker (value) VALUES (?)", (marker,))


def _database_marker(path: Path) -> str:
    with sqlite3.connect(path) as connection:
        row = connection.execute("SELECT value FROM marker").fetchone()
    assert row is not None
    return str(row[0])


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
    assert workspace.get_runs_dir() == repository / ".mini-aios" / "runs"
    assert workspace.get_skills_dir() == repository / ".mini-aios" / "skills"
    assert workspace.get_memories_dir() == repository / ".mini-aios" / "memories"
    assert workspace.get_deployments_dir() == repository / ".mini-aios" / "deployments"
    assert workspace.get_cron_logs_dir() == (
        repository / ".mini-aios" / "runs" / "cron_logs"
    )

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
    _create_sqlite_database(configured / "workspace" / "aios.db", "configured")
    configured_upload = configured / "workspace" / "session" / "chat-1" / "uploads"
    configured_upload.mkdir(parents=True)
    (configured_upload / "input.txt").write_text("input", encoding="utf-8")
    configured_artifact = (
        configured / "workspace" / "session" / "chat-1" / "artifacts"
    )
    configured_artifact.mkdir()
    (configured_artifact / "preview.html").write_text("preview", encoding="utf-8")

    monkeypatch.setattr(workspace, "_PROJECT_ROOT", repository)
    monkeypatch.setenv("AIOS_DATA_DIR", str(configured))

    assert workspace.ensure_data_dir() == configured
    assert (legacy_workspace / "do-not-move.txt").read_text(encoding="utf-8") == "live"
    assert _database_marker(configured / "state" / "aios.db") == "configured"
    assert (
        configured / "state" / "migrations" / "storage-layout-v1.json"
    ).is_file()
    assert (
        configured / "state" / "migrations" / "session-layout-v2.json"
    ).is_file()
    assert all(path.is_dir() for path in workspace._layout_directories(configured))
    assert (
        configured / "sessions" / "chat-1" / "uploads" / "input.txt"
    ).read_text(encoding="utf-8") == "input"
    assert (
        configured / "sessions" / "chat-1" / "artifacts" / "preview.html"
    ).read_text(encoding="utf-8") == "preview"
    assert not (configured / "uploads").exists()
    assert not (configured / "artifacts").exists()


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
    (repository / "uploads" / "chat-root").mkdir(parents=True)
    (repository / "artifacts" / "chat-root").mkdir(parents=True)
    (data_dir / "runs").mkdir(parents=True)

    _create_sqlite_database(old_workspace / "aios.db", "active-workspace-db")
    (old_workspace / "aios.db-wal").write_bytes(b"")
    (old_workspace / "aios.db-shm").write_bytes(b"")
    _create_sqlite_database(old_state / "aios.db", "older-state-db")
    (old_state / "aios.db-wal").write_bytes(b"")
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
    deployed_source = (
        old_workspace / "session" / "chat-1" / "files" / "deployed-app"
    )
    deployed_source.mkdir()
    (deployed_source / "app.py").write_text("deployed", encoding="utf-8")
    (old_workspace / "deploy" / "projects.json").write_text(
        json.dumps(
            {
                "deployed-app": {
                    "slug": "deployed-app",
                    "source_dir": str(deployed_source),
                    "id": "",
                    "status": "running",
                    "spec": {"run": ["python", "app.py"], "port": 8000},
                }
            }
        ),
        encoding="utf-8",
    )
    (old_workspace / "skills" / "index.json").write_text("{}", encoding="utf-8")
    (old_workspace / "runs" / "same.json").write_text("workspace", encoding="utf-8")
    (old_state / "runs" / "same.json").write_text("state", encoding="utf-8")
    (data_dir / "runs" / "same.json").write_text("canonical", encoding="utf-8")
    (repository / "memories" / "MEMORY.md").write_text("remember", encoding="utf-8")
    (repository / "uploads" / "chat-root" / "input.txt").write_text(
        "upload", encoding="utf-8"
    )
    (repository / "artifacts" / "chat-root" / "output.txt").write_text(
        "artifact", encoding="utf-8"
    )

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
    assert _database_marker(data_dir / "state" / "aios.db") == "active-workspace-db"
    assert (data_dir / "state" / "credentials.key").read_text(encoding="utf-8") == "secret"
    assert (data_dir / "sessions" / "session_manifest.json").is_file()
    assert (data_dir / "sessions" / "chat-1" / "scratch" / "draft.txt").is_file()
    assert (data_dir / "uploads" / "chat-1" / "source.pdf").is_file()
    assert (data_dir / "artifacts" / "chat-1" / "result.txt").is_file()
    assert (data_dir / "projects" / "app-1" / "app.py").is_file()
    assert (data_dir / "deployments" / "projects.json").is_file()
    deployment_registry = json.loads(
        (data_dir / "deployments" / "projects.json").read_text(encoding="utf-8")
    )
    canonical_deployed_source = data_dir / "projects" / "deployed-app"
    assert deployment_registry["deployed-app"]["source_dir"] == str(
        canonical_deployed_source
    )
    assert (canonical_deployed_source / "app.py").is_file()
    assert (data_dir / "skills" / "index.json").is_file()
    assert (data_dir / "memories" / "MEMORY.md").is_file()
    assert (data_dir / "uploads" / "chat-root" / "input.txt").is_file()
    assert (data_dir / "artifacts" / "chat-root" / "output.txt").is_file()
    assert (data_dir / "runs" / "same.json").read_text(encoding="utf-8") == "canonical"

    archive = data_dir / "legacy" / "storage-layout-v1"
    assert _database_marker(archive / "state" / "aios.db") == "older-state-db"
    assert _database_marker(archive / "workspace" / "aios.db") == "active-workspace-db"
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


def test_session_layout_nests_uploads_and_artifacts(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / ".mini-aios"
    canonical_uploads = data_dir / "sessions" / "chat-1" / "uploads"
    canonical_uploads.mkdir(parents=True)
    (canonical_uploads / "same.txt").write_text("canonical", encoding="utf-8")
    legacy_uploads = data_dir / "uploads" / "chat-1"
    legacy_uploads.mkdir(parents=True)
    (legacy_uploads / "input.txt").write_text("upload", encoding="utf-8")
    (legacy_uploads / "same.txt").write_text("legacy", encoding="utf-8")
    (data_dir / "artifacts" / "chat-1").mkdir(parents=True)
    (data_dir / "artifacts" / "chat-1" / "preview.html").write_text(
        "preview",
        encoding="utf-8",
    )
    nested_artifacts = data_dir / "sessions" / "chat-2" / "artifacts"
    nested_artifacts.mkdir(parents=True)
    (nested_artifacts / "result.txt").write_text("result", encoding="utf-8")

    report = workspace.migrate_session_layout(data_dir=data_dir)

    assert report["version"] == 2
    assert report["migration"] == "session-layout-v2"
    assert report["status"] == "complete"
    assert (canonical_uploads / "input.txt").read_text(encoding="utf-8") == "upload"
    assert (canonical_uploads / "same.txt").read_text(encoding="utf-8") == "canonical"
    backup_root = data_dir / "state" / "migration-backups" / "session-layout-v2"
    assert (backup_root / "uploads" / "chat-1" / "same.txt").read_text(
        encoding="utf-8"
    ) == "legacy"
    assert (
        data_dir / "sessions" / "chat-1" / "artifacts" / "preview.html"
    ).is_file()
    assert nested_artifacts.joinpath("result.txt").is_file()
    assert not (data_dir / "uploads").exists()
    assert not (data_dir / "artifacts").exists()

    report_path = data_dir / "state" / "migrations" / "session-layout-v2.json"
    assert json.loads(report_path.read_text(encoding="utf-8")) == report
    assert workspace.migrate_session_layout(data_dir=data_dir) == report

    late_uploads = data_dir / "uploads" / "chat-3"
    late_uploads.mkdir(parents=True)
    (late_uploads / "later.txt").write_text("later", encoding="utf-8")
    repaired = workspace.migrate_session_layout(data_dir=data_dir)
    assert (
        data_dir / "sessions" / "chat-3" / "uploads" / "later.txt"
    ).is_file()
    assert "repairedAt" in repaired


def test_production_migration_archives_preexisting_state_database(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / ".mini-aios"
    (data_dir / "state").mkdir(parents=True)
    (data_dir / "workspace").mkdir()
    _create_sqlite_database(data_dir / "state" / "aios.db", "older-state-db")
    (data_dir / "state" / "credentials.key").write_text("secret", encoding="utf-8")
    _create_sqlite_database(
        data_dir / "workspace" / "aios.db", "active-workspace-db"
    )

    workspace.migrate_storage_layout(
        data_dir=data_dir,
        project_root=tmp_path / "unused-repository",
        production=True,
    )

    assert _database_marker(data_dir / "state" / "aios.db") == "active-workspace-db"
    assert (data_dir / "state" / "credentials.key").read_text(encoding="utf-8") == "secret"
    assert _database_marker(
        data_dir / "legacy" / "storage-layout-v1" / "state" / "aios.db"
    ) == "older-state-db"


def test_database_promotion_resumes_after_sidecar_archive_interruption(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository = tmp_path / "mini-aios"
    workspace_dir = repository / "workspace"
    data_dir = repository / ".mini-aios"
    _create_sqlite_database(workspace_dir / "aios.db", "active")
    (workspace_dir / "aios.db-wal").write_bytes(b"")
    (workspace_dir / "aios.db-shm").write_bytes(b"")

    original_archive_path = workspace._archive_path
    interrupted = False

    def interrupt_once(source, *args, **kwargs):
        nonlocal interrupted
        if source == workspace_dir / "aios.db-wal" and not interrupted:
            interrupted = True
            raise RuntimeError("simulated interruption")
        return original_archive_path(source, *args, **kwargs)

    monkeypatch.setattr(workspace, "_archive_path", interrupt_once)
    with pytest.raises(RuntimeError, match="simulated interruption"):
        workspace.migrate_storage_layout(
            data_dir=data_dir,
            project_root=repository,
            production=False,
        )

    assert _database_marker(data_dir / "state" / "aios.db") == "active"
    assert not (workspace_dir / "aios.db").exists()
    assert (workspace_dir / "aios.db-wal").exists()
    in_progress = json.loads(
        (
            data_dir / "state" / "migrations" / "storage-layout-v1.json"
        ).read_text(encoding="utf-8")
    )
    assert in_progress["status"] == "in_progress"

    monkeypatch.setattr(workspace, "_archive_path", original_archive_path)
    report = workspace.migrate_storage_layout(
        data_dir=data_dir,
        project_root=repository,
        production=False,
    )

    assert report["status"] == "complete"
    assert report["resumedAt"]
    assert not any((workspace_dir / name).exists() for name in workspace._DATABASE_FILES)
    assert _database_marker(data_dir / "state" / "aios.db") == "active"


def test_migration_rejects_a_report_for_another_data_root(tmp_path: Path) -> None:
    repository = tmp_path / "mini-aios"
    data_dir = repository / ".mini-aios"
    report_path = data_dir / "state" / "migrations" / "storage-layout-v1.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(
        json.dumps(
            {
                "version": 1,
                "migration": "storage-layout-v1",
                "dataRoot": str(tmp_path / "different-root"),
                "status": "complete",
                "actions": [],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="does not match this data root"):
        workspace.migrate_storage_layout(
            data_dir=data_dir,
            project_root=repository,
            production=False,
        )


def test_completed_v1_layout_receives_idempotent_finalizers(tmp_path: Path) -> None:
    repository = tmp_path / "mini-aios"
    data_dir = repository / ".mini-aios"
    old_source = repository / "workspace" / "session" / "chat-1" / "files" / "app"
    migrated_scratch_source = data_dir / "sessions" / "chat-1" / "scratch" / "app"
    canonical_source = data_dir / "projects" / "app"
    migrated_scratch_source.mkdir(parents=True)
    (migrated_scratch_source / "app.py").write_text("app", encoding="utf-8")
    registry_path = data_dir / "deployments" / "projects.json"
    registry_path.parent.mkdir(parents=True)
    registry_path.write_text(
        json.dumps({"app": {"source_dir": str(old_source)}}),
        encoding="utf-8",
    )
    (data_dir / "cron_logs").mkdir(parents=True)
    (repository / "uploads" / "chat-1").mkdir(parents=True)
    (repository / "uploads" / "chat-1" / "input.txt").write_text(
        "upload", encoding="utf-8"
    )
    report_path = data_dir / "state" / "migrations" / "storage-layout-v1.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(
        json.dumps(
            {
                "version": 1,
                "migration": "storage-layout-v1",
                "dataRoot": str(data_dir),
                "status": "complete",
                "actions": [],
            }
        ),
        encoding="utf-8",
    )

    repaired = workspace.migrate_storage_layout(
        data_dir=data_dir,
        project_root=repository,
        production=False,
    )

    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    assert registry["app"]["source_dir"] == str(canonical_source)
    assert (canonical_source / "app.py").is_file()
    assert not migrated_scratch_source.exists()
    assert (data_dir / "runs" / "cron_logs").is_dir()
    assert (data_dir / "uploads" / "chat-1" / "input.txt").is_file()
    assert repaired["repairedAt"]
    assert workspace.migrate_storage_layout(
        data_dir=data_dir,
        project_root=repository,
        production=False,
    ) == repaired


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
    with pytest.raises(ValueError, match="cannot escape"):
        workspace.resolve_workspace_path("../outside.txt")


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
