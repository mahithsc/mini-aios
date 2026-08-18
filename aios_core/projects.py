"""Minimal SQLite-backed durable projects.

A project owns one stable directory under ``projects/<project-id>``. SQLite
owns its identity and display name; the only file Mini AIOS creates inside the
directory is ``project.md``. Everything else is chosen by the agent while it
works on the project.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .db import DB_PATH, get_db_connection, initialize_app_db
from .workspace import get_projects_dir

PROJECT_DOCUMENT_NAME = "project.md"
PROJECT_NAME_LIMIT = 200
_PROJECT_ID = re.compile(r"^proj_[0-9a-f]{32}$")


def _initial_project_document(name: str) -> str:
    return (
        f"# {name}\n\n"
        "This file is the living description and running documentation for "
        "this project.\n"
        "Keep it updated with the project's purpose, important decisions, "
        "current state, and notes that will help future work.\n"
    )


class ProjectError(RuntimeError):
    """A project lifecycle operation could not be completed safely."""


class ProjectNotFoundError(ProjectError):
    """The requested project does not exist in the canonical database."""


@dataclass(frozen=True)
class ProjectRecord:
    id: str
    name: str
    created_at: int
    updated_at: int


def _now_ms() -> int:
    return int(time.time() * 1000)


def _validated_name(name: str | None) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ProjectError("project name is required")
    normalized = name.strip()
    if len(normalized) > PROJECT_NAME_LIMIT:
        raise ProjectError(
            f"project name cannot exceed {PROJECT_NAME_LIMIT} characters"
        )
    if any(character in normalized for character in ("\n", "\r", "\x00")):
        raise ProjectError("project name must be a single line")
    return normalized


def _validated_project_id(project_id: str | None) -> str:
    if not isinstance(project_id, str) or not _PROJECT_ID.fullmatch(project_id):
        raise ProjectError("project_id must be a Mini AIOS project ID")
    return project_id


def get_project_dir(
    project_id: str,
    *,
    projects_dir: str | Path | None = None,
) -> Path:
    """Return a contained, non-symlinked canonical project directory path."""

    validated_id = _validated_project_id(project_id)
    root = Path(projects_dir) if projects_dir is not None else get_projects_dir()
    if root.is_symlink():
        raise ProjectError(f"projects root cannot be a symbolic link: {root}")
    candidate = root / validated_id
    if candidate.is_symlink():
        raise ProjectError(f"project directory cannot be a symbolic link: {candidate}")
    try:
        candidate.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError as exc:
        raise ProjectError(f"project directory escapes the projects root: {candidate}") from exc
    return candidate


def _row_to_record(row: sqlite3.Row | tuple[object, ...]) -> ProjectRecord:
    return ProjectRecord(
        id=str(row[0]),
        name=str(row[1]),
        created_at=int(row[2]),
        updated_at=int(row[3]),
    )


def _payload(record: ProjectRecord, *, projects_dir: str | Path | None) -> dict:
    project_dir = get_project_dir(record.id, projects_dir=projects_dir)
    return {
        "id": record.id,
        "name": record.name,
        "path": str(project_dir.resolve(strict=False)),
        "project_md": str((project_dir / PROJECT_DOCUMENT_NAME).resolve(strict=False)),
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "directory_exists": project_dir.is_dir(),
    }


def _initialize(db_path: str) -> None:
    initialize_app_db(db_path)


def create_project(
    name: str,
    *,
    db_path: str = DB_PATH,
    projects_dir: str | Path | None = None,
) -> dict:
    """Create a database row and a minimal project directory."""

    normalized_name = _validated_name(name)
    project_id = f"proj_{uuid4().hex}"
    project_dir = get_project_dir(project_id, projects_dir=projects_dir)
    projects_root = project_dir.parent
    projects_root.mkdir(parents=True, exist_ok=True)
    # Revalidate after creation so a pre-existing or concurrently replaced root
    # cannot redirect the project outside the intended projects directory.
    project_dir = get_project_dir(project_id, projects_dir=projects_root)
    temporary_dir = projects_root / f".{project_id}.{uuid4().hex}.tmp"
    temporary_dir.mkdir()
    (temporary_dir / PROJECT_DOCUMENT_NAME).write_text(
        _initial_project_document(normalized_name),
        encoding="utf-8",
    )

    created_at = _now_ms()
    installed = False
    try:
        _initialize(db_path)
        with get_db_connection(db_path) as connection:
            connection.execute(
                """
                INSERT INTO projects (id, name, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (project_id, normalized_name, created_at, created_at),
            )
            if project_dir.exists() or project_dir.is_symlink():
                raise ProjectError(f"project directory already exists: {project_dir}")
            temporary_dir.replace(project_dir)
            installed = True
    except BaseException:
        if installed:
            shutil.rmtree(project_dir, ignore_errors=True)
        shutil.rmtree(temporary_dir, ignore_errors=True)
        raise

    return {
        "success": True,
        "project": _payload(
            ProjectRecord(
                id=project_id,
                name=normalized_name,
                created_at=created_at,
                updated_at=created_at,
            ),
            projects_dir=projects_dir,
        ),
    }


def get_project(
    project_id: str,
    *,
    db_path: str = DB_PATH,
    projects_dir: str | Path | None = None,
) -> dict:
    validated_id = _validated_project_id(project_id)
    _initialize(db_path)
    with get_db_connection(db_path) as connection:
        row = connection.execute(
            "SELECT id, name, created_at, updated_at FROM projects WHERE id = ?",
            (validated_id,),
        ).fetchone()
    if row is None:
        raise ProjectNotFoundError(f"unknown project: {validated_id}")
    return {"success": True, "project": _payload(_row_to_record(row), projects_dir=projects_dir)}


def list_projects(
    *,
    db_path: str = DB_PATH,
    projects_dir: str | Path | None = None,
) -> dict:
    _initialize(db_path)
    with get_db_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT id, name, created_at, updated_at
            FROM projects
            ORDER BY updated_at DESC, created_at DESC, id
            """
        ).fetchall()
    return {
        "success": True,
        "projects": [
            _payload(_row_to_record(row), projects_dir=projects_dir) for row in rows
        ],
    }


def update_project(
    project_id: str,
    name: str,
    *,
    db_path: str = DB_PATH,
    projects_dir: str | Path | None = None,
) -> dict:
    """Update database-owned project metadata without rewriting project.md."""

    validated_id = _validated_project_id(project_id)
    normalized_name = _validated_name(name)
    updated_at = _now_ms()
    _initialize(db_path)
    with get_db_connection(db_path) as connection:
        cursor = connection.execute(
            "UPDATE projects SET name = ?, updated_at = ? WHERE id = ?",
            (normalized_name, updated_at, validated_id),
        )
        if cursor.rowcount != 1:
            raise ProjectNotFoundError(f"unknown project: {validated_id}")
        row = connection.execute(
            "SELECT id, name, created_at, updated_at FROM projects WHERE id = ?",
            (validated_id,),
        ).fetchone()
    assert row is not None
    return {"success": True, "project": _payload(_row_to_record(row), projects_dir=projects_dir)}


def delete_project(
    project_id: str,
    *,
    db_path: str = DB_PATH,
    projects_dir: str | Path | None = None,
) -> dict:
    """Delete a project row and its entire canonical directory."""

    validated_id = _validated_project_id(project_id)
    project_dir = get_project_dir(validated_id, projects_dir=projects_dir)
    _initialize(db_path)
    staged_dir: Path | None = None
    try:
        with get_db_connection(db_path) as connection:
            row = connection.execute(
                "SELECT id, name, created_at, updated_at FROM projects WHERE id = ?",
                (validated_id,),
            ).fetchone()
            if row is None:
                raise ProjectNotFoundError(f"unknown project: {validated_id}")
            if project_dir.exists():
                if project_dir.is_symlink() or not project_dir.is_dir():
                    raise ProjectError(
                        f"project directory is not a safe directory: {project_dir}"
                    )
                staged_dir = project_dir.with_name(
                    f".{validated_id}.{uuid4().hex}.deleting"
                )
                project_dir.replace(staged_dir)
            connection.execute("DELETE FROM projects WHERE id = ?", (validated_id,))
    except BaseException:
        if staged_dir is not None and staged_dir.exists() and not project_dir.exists():
            staged_dir.replace(project_dir)
        raise

    cleanup_warning = None
    if staged_dir is not None:
        try:
            shutil.rmtree(staged_dir)
        except OSError as exc:
            cleanup_warning = f"project data cleanup is pending at {staged_dir}: {exc}"

    result = {"success": True, "deleted_project_id": validated_id}
    if cleanup_warning:
        result["warning"] = cleanup_warning
    return result


__all__ = [
    "PROJECT_DOCUMENT_NAME",
    "ProjectError",
    "ProjectNotFoundError",
    "create_project",
    "delete_project",
    "get_project",
    "get_project_dir",
    "list_projects",
    "update_project",
]
