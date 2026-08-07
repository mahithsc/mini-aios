from __future__ import annotations

import asyncio
import io
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi import UploadFile

from aios_core import db
from aios_core.runtime_context import pop_chat_runtime_context, push_chat_runtime_context
from aios_core.storage_migration import migrate_legacy_storage
from aios_core.tools.canvas import show_canvas
from aios_core.tools import execution_sandbox
from aios_core.tools.execution_sandbox import ExecutionSandboxUnavailable
from aios_core.tools.filesystem import read, write
from aios_core.tools.skill import read_skill
from aios_core.workspace import (
    PathAccessError,
    get_runtime_paths,
    resolve_agent_path,
)
from server.uploads import save_uploads


@pytest.fixture
def isolated_layout(tmp_path, monkeypatch):
    root = tmp_path / "aios"
    monkeypatch.setenv("AIOS_HOME", str(root))
    monkeypatch.delenv("AIOS_STATE_DIR", raising=False)
    monkeypatch.delenv("AIOS_SKILLS_DIR", raising=False)
    monkeypatch.delenv("AIOS_WORKSPACE_DIR", raising=False)
    return get_runtime_paths()


def test_runtime_directories_must_not_overlap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "runtime"
    monkeypatch.setenv("AIOS_HOME", str(root))
    monkeypatch.setenv("AIOS_STATE_DIR", str(root / "workspace" / "state"))

    with pytest.raises(
        ValueError, match="state and workspace directories must not overlap"
    ):
        get_runtime_paths()


def test_agent_shell_sandbox_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AIOS_ALLOW_UNSANDBOXED_EXECUTION", raising=False)
    monkeypatch.setattr(
        execution_sandbox,
        "_native_sandbox_command",
        lambda: None,
    )

    with pytest.raises(ExecutionSandboxUnavailable):
        execution_sandbox.sandboxed_command(["/usr/bin/true"])

    assert execution_sandbox.sandboxed_command(
        ["/usr/bin/true"],
        allow_unwrapped=True,
    ) == ["/usr/bin/true"]


def test_chats_share_applications_and_sources_are_read_only(isolated_layout):
    paths = isolated_layout
    for directory in (
        paths.applications,
        paths.uploads,
        paths.downloads,
        paths.skills,
        paths.state,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    first_token = push_chat_runtime_context("chat-a")
    try:
        result = write("shared.txt", "hello from chat a")
        assert result.startswith("ok:")
    finally:
        pop_chat_runtime_context(first_token)

    second_token = push_chat_runtime_context("chat-b")
    try:
        assert "hello from chat a" in read("shared.txt")
    finally:
        pop_chat_runtime_context(second_token)

    (paths.uploads / "source.txt").write_text("original", encoding="utf-8")
    assert "original" in read("uploads/source.txt")
    assert write("uploads/source.txt", "changed").startswith(
        "error: agents may only create or modify files inside applications"
    )
    assert (paths.uploads / "source.txt").read_text(encoding="utf-8") == "original"

    (paths.downloads / "result.txt").write_text("download", encoding="utf-8")
    assert "download" in read("downloads/result.txt")
    assert write("downloads/result.txt", "changed").startswith(
        "error: agents may only create or modify files inside applications"
    )

    (paths.skills / "guide.md").write_text("instructions", encoding="utf-8")
    assert "instructions" in read("skills/guide.md")
    assert write("skills/guide.md", "changed").startswith(
        "error: agents may only create or modify files inside applications"
    )

    with pytest.raises(PathAccessError):
        resolve_agent_path(paths.state / "aios.db")
    (paths.applications / "state-link").symlink_to(paths.state, target_is_directory=True)
    with pytest.raises(PathAccessError):
        resolve_agent_path("state-link/aios.db")

    html_path = paths.applications / "preview.html"
    html_path.write_text("<h1>Preview</h1>", encoding="utf-8")
    canvas = show_canvas(kind="html", file_path="preview.html")
    assert canvas["artifact"]["filePath"] == str(html_path)
    assert canvas["artifact"]["mimeType"] == "text/html"


def test_uploads_are_shared_and_use_simple_collision_names(isolated_layout):
    paths = isolated_layout
    paths.uploads.mkdir(parents=True, exist_ok=True)

    first = asyncio.run(
        save_uploads(
            "chat-a",
            [
                UploadFile(
                    filename="report.txt",
                    file=io.BytesIO(b"first"),
                    headers={"content-type": "text/plain"},
                )
            ],
        )
    )
    second = asyncio.run(
        save_uploads(
            "chat-b",
            [
                UploadFile(
                    filename="report.txt",
                    file=io.BytesIO(b"second"),
                    headers={"content-type": "text/plain"},
                )
            ],
        )
    )

    assert first[0].filePath == "uploads/report.txt"
    assert second[0].filePath == "uploads/report 2.txt"
    assert (paths.uploads / "report.txt").read_bytes() == b"first"
    assert (paths.uploads / "report 2.txt").read_bytes() == b"second"


def test_read_skill_lists_and_reads_external_skills(isolated_layout):
    paths = isolated_layout
    skill_dir = paths.skills / "research"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: research\n"
        "description: Research a topic carefully.\n"
        "---\n\n"
        "# Research\n\n"
        "Verify important claims.\n",
        encoding="utf-8",
    )

    listed = read_skill()
    loaded = read_skill("Research")
    missing = read_skill("researc")

    assert listed["count"] == 1
    assert listed["skills"][0]["name"] == "research"
    assert loaded["skill"]["name"] == "research"
    assert "Verify important claims." in loaded["instructions"]
    assert loaded["truncated"] is False
    assert missing["suggestions"] == ["research"]


def test_destructive_migration_requires_preserved_database(isolated_layout):
    paths = isolated_layout
    (paths.workspace / "session" / "chat-1").mkdir(parents=True)
    legacy_file = paths.workspace / "session" / "chat-1" / "chat.json"
    legacy_file.write_text("[]", encoding="utf-8")

    with pytest.raises(
        RuntimeError,
        match="before the active database exists",
    ):
        migrate_legacy_storage(paths=paths, project_root=paths.root)

    assert legacy_file.exists()


def test_legacy_storage_is_purged_to_clean_shared_layout(isolated_layout):
    paths = isolated_layout
    project_root = paths.root
    legacy_workspace = paths.workspace

    session = legacy_workspace / "session" / "chat-1"
    (session / "uploads").mkdir(parents=True)
    (session / "files").mkdir()
    (session / "artifacts").mkdir()
    (session / "uploads" / "report.pdf").write_bytes(b"pdf")
    (session / "files" / "notes.txt").write_text("notes", encoding="utf-8")
    (session / "artifacts" / "index.html").write_text(
        "<h1>artifact</h1>",
        encoding="utf-8",
    )
    (session / "chat.json").write_text("[]", encoding="utf-8")

    (legacy_workspace / "runs" / "events").mkdir(parents=True)
    (legacy_workspace / "runs" / "events" / "run-1.jsonl").write_text(
        "{}\n",
        encoding="utf-8",
    )
    (legacy_workspace / "cron_logs").mkdir()
    (legacy_workspace / "cron_logs" / "cron.log").write_text(
        "complete",
        encoding="utf-8",
    )
    (legacy_workspace / "skills" / "custom").mkdir(parents=True)
    (legacy_workspace / "skills" / "custom" / "SKILL.md").write_text(
        "# Custom",
        encoding="utf-8",
    )
    (legacy_workspace / "skills" / "skills_index.json").write_text(
        json.dumps(
            {
                "version": 1,
                "skills": [
                    {
                        "id": "custom",
                        "title": "Custom",
                        "file": "skills/custom/SKILL.md",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    paths.skills.mkdir(parents=True)
    (paths.skills / "skills_index.json").write_text(
        json.dumps(
            {
                "version": 1,
                "skills": [
                    {
                        "id": "existing",
                        "title": "Existing",
                        "file": "skills/existing/SKILL.md",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (legacy_workspace / "calculator.py").write_text(
        "print(2 + 2)\n",
        encoding="utf-8",
    )
    (paths.applications / "current").mkdir(parents=True)
    (paths.applications / "current" / "keep.txt").write_text(
        "keep",
        encoding="utf-8",
    )
    (paths.applications / "recovered" / "old-chat").mkdir(parents=True)
    (paths.applications / "recovered" / "old-chat" / "old.txt").write_text(
        "delete",
        encoding="utf-8",
    )
    paths.uploads.mkdir()
    (paths.uploads / "old-upload.txt").write_text("delete", encoding="utf-8")

    (project_root / "uploads" / "chat-2").mkdir(parents=True)
    (project_root / "uploads" / "chat-2" / "other.pdf").write_bytes(b"other")
    (project_root / "heartbeat_logs").mkdir()
    (project_root / "heartbeat_logs" / "old.log").write_text(
        "old",
        encoding="utf-8",
    )

    paths.state.mkdir(parents=True)
    with sqlite3.connect(paths.database) as connection:
        connection.executescript(
            """
            CREATE TABLE message_attachments (
                id TEXT PRIMARY KEY,
                file_path TEXT NOT NULL
            );
            CREATE TABLE attachment_representations (
                id TEXT PRIMARY KEY,
                file_path TEXT
            );
            """
        )
        connection.executemany(
            "INSERT INTO message_attachments (id, file_path) VALUES (?, ?)",
            (
                (
                    "attachment-1",
                    "workspace/session/chat-1/uploads/report.pdf",
                ),
                ("attachment-2", "uploads/chat-2/other.pdf"),
            ),
        )

    first = migrate_legacy_storage(paths=paths, project_root=project_root)
    (paths.uploads / "new-upload.txt").write_text("new", encoding="utf-8")
    second = migrate_legacy_storage(paths=paths, project_root=project_root)

    assert first.already_migrated is False
    assert second.already_migrated is True
    assert {entry.name for entry in paths.workspace.iterdir()} == {
        "applications",
        "uploads",
        "downloads",
    }
    assert (paths.applications / "current" / "keep.txt").read_text(
        encoding="utf-8"
    ) == "keep"
    assert not (paths.applications / "recovered").exists()
    assert [entry.name for entry in paths.uploads.iterdir()] == ["new-upload.txt"]
    assert list(paths.downloads.iterdir()) == []
    assert list(paths.runs.iterdir()) == []
    assert list(paths.cron_logs.iterdir()) == []
    assert list(paths.heartbeat_logs.iterdir()) == []
    assert list(paths.assistants.iterdir()) == []
    assert not (project_root / "uploads").exists()
    assert not (project_root / "heartbeat_logs").exists()
    assert (paths.skills / "custom" / "SKILL.md").exists()
    skill_index = json.loads(
        (paths.skills / "skills_index.json").read_text(encoding="utf-8")
    )
    assert [entry["id"] for entry in skill_index["skills"]] == [
        "custom",
        "existing",
    ]
    assert not (paths.skills / "skills_index 2.json").exists()
    assert (paths.skills / "skills_index_pre_migration.json").exists()
    assert not (paths.state / "legacy_workspace").exists()
    assert (paths.state / "storage-layout-v2.json").exists()
    assert first.deleted_files > 0
    assert first.deleted_directories > 0

    with sqlite3.connect(paths.database) as connection:
        preserved_paths = dict(
            connection.execute(
                "SELECT id, file_path FROM message_attachments ORDER BY id"
            )
        )
    assert preserved_paths == {
        "attachment-1": "workspace/session/chat-1/uploads/report.pdf",
        "attachment-2": "uploads/chat-2/other.pdf",
    }


def test_purge_preserves_archived_skills_before_deleting_archive(isolated_layout):
    paths = isolated_layout
    archived_skills = paths.state / "legacy_workspace" / "skills"
    archived_skills.mkdir(parents=True)
    paths.skills.mkdir(parents=True)
    (paths.state / "storage-layout-v1.json").write_text("{}", encoding="utf-8")
    with sqlite3.connect(paths.database):
        pass
    (archived_skills / "skills_index.json").write_text(
        json.dumps(
            {
                "version": 1,
                "skills": [
                    {
                        "id": "recovered",
                        "file": "skills/recovered/SKILL.md",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (paths.skills / "skills_index.json").write_text(
        json.dumps({"version": 1, "skills": []}),
        encoding="utf-8",
    )

    report = migrate_legacy_storage(paths=paths, project_root=paths.root)

    active_index = json.loads(
        (paths.skills / "skills_index.json").read_text(encoding="utf-8")
    )
    assert report.already_migrated is False
    assert active_index["skills"] == [
        {
            "id": "recovered",
            "file": "skills/recovered/SKILL.md",
        }
    ]
    assert not archived_skills.exists()
    assert not (paths.state / "storage-layout-v1.json").exists()
    assert (paths.state / "storage-layout-v2.json").exists()


def test_legacy_database_backup_includes_committed_wal_data(tmp_path, monkeypatch):
    source = tmp_path / "legacy.db"
    target = tmp_path / "state" / "aios.db"
    with sqlite3.connect(source) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("CREATE TABLE records (value TEXT)")
        connection.execute("INSERT INTO records VALUES ('preserved')")

    monkeypatch.setattr(db, "DB_PATH", str(target))
    monkeypatch.setattr(db, "_legacy_db_candidates", lambda: [source])

    with db.get_db_connection(str(target)) as connection:
        value = connection.execute("SELECT value FROM records").fetchone()[0]

    assert value == "preserved"


def test_split_legacy_cron_database_is_merged_without_id_collisions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main_source = tmp_path / "aios.db"
    cron_source = tmp_path / "crons.db"
    target = tmp_path / "state" / "aios.db"
    cron_schema = """
        CREATE TABLE crons (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            instructions TEXT NOT NULL,
            schedule TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_run_at TEXT
        );
        CREATE TABLE cron_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cron_id TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            output TEXT,
            status TEXT NOT NULL
        );
    """
    with sqlite3.connect(main_source) as connection:
        connection.executescript(cron_schema)
        connection.execute(
            """
            INSERT INTO crons VALUES (
                'main-cron', 'Main', '', 'main', '* * * * *',
                'active', '2026-01-01', NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO cron_runs VALUES (
                1, 'main-cron', '2026-01-01', NULL, NULL, 'completed'
            )
            """
        )
    with sqlite3.connect(cron_source) as connection:
        connection.executescript(cron_schema)
        connection.execute(
            """
            INSERT INTO crons VALUES (
                'split-cron', 'Split', '', 'split', '0 9 * * *',
                'active', '2026-02-01', NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO cron_runs VALUES (
                1, 'split-cron', '2026-02-01', NULL, 'done', 'completed'
            )
            """
        )

    monkeypatch.setattr(db, "DB_PATH", str(target))
    monkeypatch.setattr(
        db,
        "_legacy_db_candidates",
        lambda: [main_source, cron_source],
    )

    db.initialize_app_db(str(target))
    db.initialize_app_db(str(target))

    with sqlite3.connect(target) as connection:
        cron_ids = {
            row[0]
            for row in connection.execute("SELECT id FROM crons")
        }
        run_rows = connection.execute(
            "SELECT cron_id, started_at FROM cron_runs ORDER BY cron_id"
        ).fetchall()
        timezone = connection.execute(
            "SELECT schedule_timezone FROM crons WHERE id = 'split-cron'"
        ).fetchone()[0]

    assert cron_ids == {"main-cron", "split-cron"}
    assert run_rows == [
        ("main-cron", "2026-01-01"),
        ("split-cron", "2026-02-01"),
    ]
    assert timezone == "America/New_York"
