from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_PROD_ENV_VALUES = {"prod", "production"}
_STORAGE_LAYOUT_VERSION = 1
_MIGRATION_NAME = f"storage-layout-v{_STORAGE_LAYOUT_VERSION}"
_MIGRATION_LOCK = threading.RLock()
_DATABASE_FILES = ("aios.db", "aios.db-wal", "aios.db-shm")
_LEGACY_DATABASE_FILES = ("crons.db", "crons.db-wal", "crons.db-shm")


def get_environment() -> str:
    return (
        os.getenv("AIOS_ENV")
        or os.getenv("APP_ENV")
        or os.getenv("ENV")
        or "dev"
    ).strip().lower()


def is_production() -> bool:
    return get_environment() in _PROD_ENV_VALUES


def _configured_data_dir() -> Path | None:
    configured = os.getenv("AIOS_DATA_DIR")
    if not configured:
        return None
    return Path(configured).expanduser().resolve(strict=False)


def get_data_dir() -> Path:
    """Return the one canonical root for all mutable Mini AIOS data.

    ``AIOS_DATA_DIR`` is an explicit administrative/test override. Otherwise
    development uses ``<repository>/.mini-aios`` and production uses
    ``~/.mini-aios``.
    """

    configured = _configured_data_dir()
    if configured is not None:
        return configured
    if is_production():
        return Path("~/.mini-aios").expanduser().resolve(strict=False)
    return _PROJECT_ROOT / ".mini-aios"


def get_state_dir() -> Path:
    return get_data_dir() / "state"


def get_projects_dir() -> Path:
    return get_data_dir() / "projects"


def get_sessions_dir() -> Path:
    return get_data_dir() / "sessions"


def get_uploads_dir() -> Path:
    return get_data_dir() / "uploads"


def get_artifacts_dir() -> Path:
    return get_data_dir() / "artifacts"


def get_runs_dir() -> Path:
    return get_data_dir() / "runs"


def get_skills_dir() -> Path:
    return get_data_dir() / "skills"


def get_memories_dir() -> Path:
    return get_data_dir() / "memories"


def get_deployments_dir() -> Path:
    return get_data_dir() / "deployments"


def get_cron_logs_dir() -> Path:
    return get_data_dir() / "cron_logs"


def get_legacy_state_db_path() -> Path:
    """Return the read-only recovery source retained by layout migration v1."""

    return get_data_dir() / "legacy" / _MIGRATION_NAME / "state" / "aios.db"


def _layout_directories(data_dir: Path) -> tuple[Path, ...]:
    return tuple(
        data_dir / name
        for name in (
            "state",
            "projects",
            "sessions",
            "uploads",
            "artifacts",
            "runs",
            "skills",
            "memories",
            "deployments",
            "cron_logs",
        )
    )


def _paths_are_equal(first: Path, second: Path) -> bool:
    return first.resolve(strict=False) == second.resolve(strict=False)


def _record(
    report: dict[str, Any],
    action: str,
    source: Path,
    destination: Path,
    **details: object,
) -> None:
    entry: dict[str, object] = {
        "action": action,
        "source": str(source),
        "destination": str(destination),
    }
    entry.update(details)
    report["actions"].append(entry)


def _available_archive_path(destination: Path) -> Path:
    if not destination.exists() and not destination.is_symlink():
        return destination
    for number in range(1, 10_000):
        candidate = destination.with_name(f"{destination.name}.conflict-{number}")
        if not candidate.exists() and not candidate.is_symlink():
            return candidate
    raise RuntimeError(f"could not allocate legacy archive path for {destination}")


def _move_unchecked(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(destination))


def _archive_path(
    source: Path,
    archive_destination: Path,
    report: dict[str, Any],
    *,
    reason: str,
    canonical_destination: Path | None = None,
) -> None:
    if not source.exists() and not source.is_symlink():
        return
    destination = _available_archive_path(archive_destination)
    _move_unchecked(source, destination)
    details: dict[str, object] = {"reason": reason}
    if canonical_destination is not None:
        details["canonicalDestination"] = str(canonical_destination)
    _record(report, "archived", source, destination, **details)


def _merge_path(
    source: Path,
    destination: Path,
    archive_destination: Path,
    report: dict[str, Any],
) -> None:
    """Move a legacy path without ever replacing a canonical path."""

    if not source.exists() and not source.is_symlink():
        return
    if _paths_are_equal(source, destination):
        return

    source_is_directory = source.is_dir() and not source.is_symlink()
    destination_exists = destination.exists() or destination.is_symlink()
    destination_is_directory = destination.is_dir() and not destination.is_symlink()

    if not destination_exists:
        _move_unchecked(source, destination)
        _record(report, "moved", source, destination)
        return

    if source_is_directory and destination_is_directory:
        for child in sorted(source.iterdir(), key=lambda path: path.name):
            _merge_path(
                child,
                destination / child.name,
                archive_destination / child.name,
                report,
            )
        try:
            source.rmdir()
        except OSError:
            pass
        return

    _archive_path(
        source,
        archive_destination,
        report,
        reason="canonical destination already exists",
        canonical_destination=destination,
    )


def _archive_database_group(
    source_dir: Path,
    archive_dir: Path,
    report: dict[str, Any],
    *,
    canonical_state_dir: Path,
) -> None:
    for name in _DATABASE_FILES:
        source = source_dir / name
        _archive_path(
            source,
            archive_dir / name,
            report,
            reason="superseded legacy database",
            canonical_destination=canonical_state_dir / name,
        )


def _promote_database_group(
    source_dir: Path,
    state_dir: Path,
    archive_dir: Path,
    report: dict[str, Any],
    *,
    replace_existing: bool,
) -> None:
    for name in _DATABASE_FILES:
        source = source_dir / name
        if not source.exists() and not source.is_symlink():
            continue
        destination = state_dir / name
        if destination.exists() or destination.is_symlink():
            if not replace_existing:
                _archive_path(
                    source,
                    archive_dir / name,
                    report,
                    reason="canonical database already exists",
                    canonical_destination=destination,
                )
                continue
            _archive_path(
                destination,
                archive_dir / name,
                report,
                reason="replaced by active workspace database",
                canonical_destination=destination,
            )
        _move_unchecked(source, destination)
        _record(report, "promoted-database", source, destination)


def _migrate_session_tree(
    source: Path,
    *,
    data_dir: Path,
    archive_dir: Path,
    report: dict[str, Any],
) -> None:
    if not source.is_dir() or source.is_symlink():
        _merge_path(source, data_dir / "sessions", archive_dir, report)
        return

    sessions_dir = data_dir / "sessions"
    uploads_dir = data_dir / "uploads"
    artifacts_dir = data_dir / "artifacts"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    for child in sorted(source.iterdir(), key=lambda path: path.name):
        if not child.is_dir() or child.is_symlink():
            _merge_path(
                child,
                sessions_dir / child.name,
                archive_dir / child.name,
                report,
            )
            continue

        chat_id = child.name
        chat_target = sessions_dir / chat_id
        chat_archive = archive_dir / chat_id
        for chat_child in sorted(child.iterdir(), key=lambda path: path.name):
            if chat_child.name == "files":
                destination = chat_target / "scratch"
            elif chat_child.name == "uploads":
                destination = uploads_dir / chat_id
            elif chat_child.name == "artifacts":
                destination = artifacts_dir / chat_id
            else:
                destination = chat_target / chat_child.name
            _merge_path(
                chat_child,
                destination,
                chat_archive / chat_child.name,
                report,
            )
        try:
            child.rmdir()
        except OSError:
            pass

    try:
        source.rmdir()
    except OSError:
        pass


def _migrate_workspace(
    workspace_dir: Path,
    *,
    data_dir: Path,
    archive_root: Path,
    report: dict[str, Any],
) -> None:
    if not workspace_dir.is_dir() or workspace_dir.is_symlink():
        return

    archive_dir = archive_root / "workspace"
    mappings = {
        "apps": "projects",
        "projects": "projects",
        "skills": "skills",
        "runs": "runs",
        "deploy": "deployments",
        "deployments": "deployments",
        "cron_logs": "cron_logs",
        "uploads": "uploads",
        "artifacts": "artifacts",
    }

    session_dir = workspace_dir / "session"
    if session_dir.exists() or session_dir.is_symlink():
        _migrate_session_tree(
            session_dir,
            data_dir=data_dir,
            archive_dir=archive_dir / "session",
            report=report,
        )

    for source_name, destination_name in mappings.items():
        source = workspace_dir / source_name
        _merge_path(
            source,
            data_dir / destination_name,
            archive_dir / source_name,
            report,
        )

    for name in _LEGACY_DATABASE_FILES:
        _merge_path(
            workspace_dir / name,
            data_dir / "state" / name,
            archive_dir / name,
            report,
        )

    _merge_path(
        workspace_dir / "update-drain.json",
        data_dir / "state" / "update-drain.json",
        archive_dir / "update-drain.json",
        report,
    )

    # ``applications`` was an abandoned predecessor to cloud app workspaces.
    # Keep it for manual recovery without presenting it as a canonical project.
    _archive_path(
        workspace_dir / "applications",
        archive_dir / "applications",
        report,
        reason="unmapped legacy application storage",
    )

    handled = {
        "session",
        "applications",
        "update-drain.json",
        *_DATABASE_FILES,
        *_LEGACY_DATABASE_FILES,
        *mappings,
    }
    for child in sorted(workspace_dir.iterdir(), key=lambda path: path.name):
        if child.name in handled:
            continue
        _archive_path(
            child,
            archive_dir / child.name,
            report,
            reason="unmapped legacy workspace data",
        )

    try:
        workspace_dir.rmdir()
    except OSError:
        pass


def _write_report(report_path: Path, report: dict[str, Any]) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{_MIGRATION_NAME}.",
        dir=str(report_path.parent),
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, report_path)
    finally:
        Path(temporary_name).unlink(missing_ok=True)


def migrate_storage_layout(
    *,
    data_dir: Path | None = None,
    project_root: Path | None = None,
    production: bool | None = None,
) -> dict[str, Any]:
    """Move legacy storage into the canonical layout exactly once.

    The active legacy ``workspace/aios.db`` takes precedence over the older
    ``state/aios.db``. Every other collision is resolved in favor of data that
    is already canonical, with the legacy side retained below
    ``legacy/storage-layout-v1``. Callers can inject roots for tests or an
    explicit administrative migration.
    """

    canonical_root = (data_dir or get_data_dir()).expanduser().resolve(strict=False)
    source_root = (project_root or _PROJECT_ROOT).expanduser().resolve(strict=False)
    production_mode = is_production() if production is None else production
    state_dir = canonical_root / "state"
    report_path = state_dir / "migrations" / f"{_MIGRATION_NAME}.json"

    with _MIGRATION_LOCK:
        if report_path.is_file():
            try:
                existing = json.loads(report_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                existing = None
            if isinstance(existing, dict) and existing.get("version") == _STORAGE_LAYOUT_VERSION:
                return existing

        canonical_root.mkdir(parents=True, exist_ok=True)
        state_dir.mkdir(parents=True, exist_ok=True)
        archive_root = canonical_root / "legacy" / _MIGRATION_NAME
        report: dict[str, Any] = {
            "version": _STORAGE_LAYOUT_VERSION,
            "migration": _MIGRATION_NAME,
            "dataRoot": str(canonical_root),
            "startedAt": datetime.now(timezone.utc).isoformat(),
            "actions": [],
        }

        if production_mode:
            workspace_dir = canonical_root / "workspace"
            legacy_memories_dir = canonical_root / "memories"
            legacy_state_dir = state_dir
        else:
            workspace_dir = source_root / "workspace"
            legacy_memories_dir = source_root / "memories"
            legacy_state_dir = source_root / "state"

        active_workspace_db = workspace_dir / "aios.db"
        if active_workspace_db.is_file() or active_workspace_db.is_symlink():
            legacy_database_source = legacy_state_dir / "aios.db"
            if not legacy_database_source.exists():
                legacy_database_source = state_dir / "aios.db"
            if legacy_database_source.exists():
                report["legacyDatabaseDisposition"] = {
                    "source": str(legacy_database_source),
                    "archive": str(archive_root / "state" / "aios.db"),
                    "canonicalSource": str(active_workspace_db),
                    "destinationWinsImports": [
                        "chats",
                        "crons",
                        "cron_runs",
                        "device_link",
                    ],
                    "archiveOnly": [
                        "gateway_events",
                        "unrecognized tables",
                    ],
                }
            if not _paths_are_equal(legacy_state_dir, state_dir):
                _archive_database_group(
                    legacy_state_dir,
                    archive_root / "state",
                    report,
                    canonical_state_dir=state_dir,
                )
            _archive_database_group(
                state_dir,
                archive_root / "state",
                report,
                canonical_state_dir=state_dir,
            )
            _promote_database_group(
                workspace_dir,
                state_dir,
                archive_root / "workspace",
                report,
                replace_existing=True,
            )
        else:
            canonical_db = state_dir / "aios.db"
            legacy_db = legacy_state_dir / "aios.db"
            if not canonical_db.exists() and legacy_db.exists():
                _promote_database_group(
                    legacy_state_dir,
                    state_dir,
                    archive_root / "state",
                    report,
                    replace_existing=False,
                )
            elif not _paths_are_equal(legacy_state_dir, state_dir) and legacy_db.exists():
                _archive_database_group(
                    legacy_state_dir,
                    archive_root / "state",
                    report,
                    canonical_state_dir=state_dir,
                )

        _migrate_workspace(
            workspace_dir,
            data_dir=canonical_root,
            archive_root=archive_root,
            report=report,
        )

        if not _paths_are_equal(legacy_state_dir, state_dir) and legacy_state_dir.is_dir():
            _merge_path(
                legacy_state_dir / "runs",
                canonical_root / "runs",
                archive_root / "state" / "runs",
                report,
            )
            for child in sorted(legacy_state_dir.iterdir(), key=lambda path: path.name):
                if child.name in _DATABASE_FILES or child.name == "runs":
                    continue
                _merge_path(
                    child,
                    state_dir / child.name,
                    archive_root / "state" / child.name,
                    report,
                )
            try:
                legacy_state_dir.rmdir()
            except OSError:
                pass
        elif legacy_state_dir.is_dir():
            _merge_path(
                legacy_state_dir / "runs",
                canonical_root / "runs",
                archive_root / "state" / "runs",
                report,
            )

        _merge_path(
            legacy_memories_dir,
            canonical_root / "memories",
            archive_root / "memories",
            report,
        )

        if not production_mode:
            root_session = source_root / "session"
            if root_session.exists() or root_session.is_symlink():
                _migrate_session_tree(
                    root_session,
                    data_dir=canonical_root,
                    archive_dir=archive_root / "root-session",
                    report=report,
                )
            for source_name, destination_name in (
                ("skills", "skills"),
                ("runs", "runs"),
                ("cron_logs", "cron_logs"),
            ):
                _merge_path(
                    source_root / source_name,
                    canonical_root / destination_name,
                    archive_root / f"root-{source_name}",
                    report,
                )
            if not (state_dir / "aios.db").exists():
                _promote_database_group(
                    source_root,
                    state_dir,
                    archive_root / "root",
                    report,
                    replace_existing=False,
                )
            for name in _LEGACY_DATABASE_FILES:
                _merge_path(
                    source_root / name,
                    state_dir / name,
                    archive_root / "root" / name,
                    report,
                )

        for directory in _layout_directories(canonical_root):
            directory.mkdir(parents=True, exist_ok=True)

        report["completedAt"] = datetime.now(timezone.utc).isoformat()
        report["status"] = "complete"
        _write_report(report_path, report)
        return report


def ensure_data_dir() -> Path:
    data_dir = get_data_dir()
    # An explicit override is deliberately isolated: importing Mini AIOS with
    # a temporary/test root must never sweep legacy data out of the checkout.
    # Administrative migrations can call migrate_storage_layout with injected
    # roots before initialization.
    if _configured_data_dir() is None:
        migrate_storage_layout(data_dir=data_dir)
    else:
        data_dir.mkdir(parents=True, exist_ok=True)
        for directory in _layout_directories(data_dir):
            directory.mkdir(parents=True, exist_ok=True)
    return data_dir


def get_workspace_dir() -> Path:
    """Compatibility alias for the canonical data root.

    New code should select a typed directory helper instead of treating all
    mutable data as an undifferentiated workspace.
    """

    return get_data_dir()


def ensure_workspace_dir() -> Path:
    """Compatibility alias for :func:`ensure_data_dir`."""

    return ensure_data_dir()


_LEGACY_PATH_PREFIXES = {
    "session": "sessions",
    "apps": "projects",
    "deploy": "deployments",
    "aios.db": "state/aios.db",
    "crons.db": "state/crons.db",
}


def resolve_workspace_path(path: str | Path) -> Path:
    """Resolve an old workspace-relative path below the canonical data root.

    A small set of historical first-segment names is translated so callers
    can migrate independently. Absolute paths retain their existing meaning.
    """

    raw_path = Path(path).expanduser()
    if raw_path.is_absolute():
        return raw_path
    parts = raw_path.parts
    if not parts or parts == (".",):
        return ensure_data_dir()
    translated = _LEGACY_PATH_PREFIXES.get(parts[0])
    if translated is not None:
        return ensure_data_dir() / Path(translated, *parts[1:])
    return ensure_data_dir() / raw_path
