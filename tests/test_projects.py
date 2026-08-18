from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from aios_core import projects
from aios_core.release import DATABASE_SCHEMA_VERSION


def _create(tmp_path: Path, name: str = "Example") -> tuple[str, Path, str]:
    db_path = str(tmp_path / "state" / "aios.db")
    projects_dir = tmp_path / "projects"
    result = projects.create_project(
        name,
        db_path=db_path,
        projects_dir=projects_dir,
    )
    return db_path, projects_dir, result["project"]["id"]


def test_project_create_is_minimal_and_sqlite_owned(tmp_path: Path) -> None:
    db_path, projects_dir, project_id = _create(tmp_path, "Example Website")
    project_dir = projects_dir / project_id

    assert sorted(path.name for path in project_dir.iterdir()) == ["project.md"]
    assert (project_dir / "project.md").read_text(encoding="utf-8") == (
        "# Example Website\n"
    )
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT id, name, created_at, updated_at FROM projects"
        ).fetchone()
        migration = connection.execute(
            "SELECT name, checksum FROM schema_migrations WHERE version = 6"
        ).fetchone()
        maximum_version = connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0]

    assert row is not None
    assert row[0] == project_id
    assert row[1] == "Example Website"
    assert row[2] == row[3]
    assert migration == ("durable_projects", "projects-v1")
    assert maximum_version == DATABASE_SCHEMA_VERSION


def test_project_get_list_and_update_only_change_database_metadata(
    tmp_path: Path,
) -> None:
    db_path, projects_dir, project_id = _create(tmp_path, "Original Name")

    fetched = projects.get_project(
        project_id,
        db_path=db_path,
        projects_dir=projects_dir,
    )
    listed = projects.list_projects(db_path=db_path, projects_dir=projects_dir)
    updated = projects.update_project(
        project_id,
        "Renamed Project",
        db_path=db_path,
        projects_dir=projects_dir,
    )

    assert fetched["project"]["name"] == "Original Name"
    assert listed["projects"] == [fetched["project"]]
    assert updated["project"]["name"] == "Renamed Project"
    # project.md is agent-owned documentation; metadata updates do not rewrite it.
    assert (projects_dir / project_id / "project.md").read_text(encoding="utf-8") == (
        "# Original Name\n"
    )


def test_project_delete_removes_database_row_and_whole_directory(
    tmp_path: Path,
) -> None:
    db_path, projects_dir, project_id = _create(tmp_path)
    project_dir = projects_dir / project_id
    (project_dir / "agent-chosen-folder").mkdir()
    (project_dir / "agent-chosen-folder" / "app.py").write_text(
        "print('hello')\n",
        encoding="utf-8",
    )

    result = projects.delete_project(
        project_id,
        db_path=db_path,
        projects_dir=projects_dir,
    )

    assert result == {"success": True, "deleted_project_id": project_id}
    assert not project_dir.exists()
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0


def test_project_delete_rejects_symlinked_project_directory(tmp_path: Path) -> None:
    db_path, projects_dir, project_id = _create(tmp_path)
    project_dir = projects_dir / project_id
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    for path in project_dir.iterdir():
        path.unlink()
    project_dir.rmdir()
    project_dir.symlink_to(outside, target_is_directory=True)

    with pytest.raises(projects.ProjectError, match="symbolic link"):
        projects.delete_project(
            project_id,
            db_path=db_path,
            projects_dir=projects_dir,
        )

    assert (outside / "keep.txt").read_text(encoding="utf-8") == "keep"
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1


def test_project_create_rejects_symlinked_projects_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    projects_dir = tmp_path / "projects"
    projects_dir.symlink_to(outside, target_is_directory=True)

    with pytest.raises(projects.ProjectError, match="projects root"):
        projects.create_project(
            "Example",
            db_path=str(tmp_path / "aios.db"),
            projects_dir=projects_dir,
        )

    assert list(outside.iterdir()) == []


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("", "required"),
        ("line one\nline two", "single line"),
        ("x" * 201, "200"),
    ],
)
def test_project_create_rejects_invalid_names(
    tmp_path: Path,
    name: str,
    message: str,
) -> None:
    with pytest.raises(projects.ProjectError, match=message):
        projects.create_project(
            name,
            db_path=str(tmp_path / "aios.db"),
            projects_dir=tmp_path / "projects",
        )
    assert not (tmp_path / "projects").exists()


def test_project_missing_and_invalid_ids_do_not_touch_filesystem(tmp_path: Path) -> None:
    db_path, projects_dir, _project_id = _create(tmp_path)

    with pytest.raises(projects.ProjectError, match="project_id"):
        projects.get_project(
            "../outside",
            db_path=db_path,
            projects_dir=projects_dir,
        )
    with pytest.raises(projects.ProjectNotFoundError, match="unknown project"):
        projects.get_project(
            f"proj_{'0' * 32}",
            db_path=db_path,
            projects_dir=projects_dir,
        )
