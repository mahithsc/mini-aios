from __future__ import annotations

import sqlite3
from pathlib import Path

from ..workspace import RuntimePaths, get_runtime_paths


class AppHostExecutionDenied(ValueError):
    """Raised when a host process tries to enter a managed App root."""


def _within(candidate: Path, root: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _database_app_roots(paths: RuntimePaths) -> set[Path]:
    database = paths.database.expanduser()
    if not database.is_file():
        return set()

    roots: set[Path] = set()
    try:
        uri = f"file:{database.resolve()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=0.25) as connection:
            rows = connection.execute("SELECT root_path FROM apps").fetchall()
    except (OSError, sqlite3.Error):
        return set()

    applications = paths.applications.resolve()
    for (raw_path,) in rows:
        if not isinstance(raw_path, str) or not raw_path.strip():
            continue
        relative = Path(raw_path.strip())
        if relative.is_absolute():
            continue
        if relative.parts and relative.parts[0].lower() == "applications":
            relative = Path(*relative.parts[1:])
        candidate = applications / relative
        if _within(candidate, applications):
            roots.add(candidate.resolve())
    return roots


def protected_app_roots(paths: RuntimePaths | None = None) -> tuple[Path, ...]:
    """Return managed App roots that host execution must not access.

    The manifest scan protects newly-created drafts before their first database
    write. The database scan keeps a registered App protected if its editable
    manifest is temporarily missing or invalid.
    """

    runtime_paths = paths or get_runtime_paths()
    applications = runtime_paths.applications
    roots = _database_app_roots(runtime_paths)
    try:
        children = tuple(applications.iterdir())
    except OSError:
        children = ()
    for child in children:
        try:
            if child.is_dir() and (child / "app.json").exists():
                roots.add(child.resolve())
        except (OSError, RuntimeError):
            continue
    return tuple(sorted(roots, key=str))


def app_root_for_path(
    path: str | Path,
    *,
    paths: RuntimePaths | None = None,
) -> Path | None:
    try:
        candidate = Path(path).resolve()
    except (OSError, RuntimeError):
        return None
    for root in protected_app_roots(paths):
        if _within(candidate, root):
            return root
    return None


def ensure_host_execution_allowed(
    path: str | Path,
    *,
    paths: RuntimePaths | None = None,
) -> None:
    root = app_root_for_path(path, paths=paths)
    if root is None:
        return
    raise AppHostExecutionDenied(
        "managed App code cannot run through bash or process tools; "
        "use the app tool so it runs in the isolated App runtime"
    )
