from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - Mini AIOS ships on macOS/Linux.
    fcntl = None

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
    return get_runs_dir() / "cron_logs"


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
            "runs/cron_logs",
        )
    )


def _paths_are_equal(first: Path, second: Path) -> bool:
    return first.resolve(strict=False) == second.resolve(strict=False)


def _plan_action(
    report: dict[str, Any],
    action: str,
    source: Path,
    destination: Path,
    **details: object,
) -> dict[str, object]:
    entry: dict[str, object] = {
        "action": action,
        "source": str(source),
        "destination": str(destination),
        "status": "planned",
    }
    entry.update(details)
    report["actions"].append(entry)
    report_path = report.get("_reportPath")
    if isinstance(report_path, str):
        _write_report(Path(report_path), report)
    return entry


def _complete_action(report: dict[str, Any], entry: dict[str, object]) -> None:
    entry["status"] = "complete"
    report_path = report.get("_reportPath")
    if isinstance(report_path, str):
        _write_report(Path(report_path), report)


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
    details: dict[str, object] = {"reason": reason}
    if canonical_destination is not None:
        details["canonicalDestination"] = str(canonical_destination)
    action = _plan_action(report, "archived", source, destination, **details)
    _move_unchecked(source, destination)
    _complete_action(report, action)


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
        action = _plan_action(report, "moved", source, destination)
        _move_unchecked(source, destination)
        _complete_action(report, action)
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


def _backup_sqlite_database(source: Path, destination: Path) -> None:
    """Create and atomically install a self-contained SQLite snapshot."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".backup",
        dir=str(destination.parent),
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        source_uri = f"{source.resolve().as_uri()}?mode=ro"
        with (
            sqlite3.connect(source_uri, uri=True, timeout=5.0) as source_connection,
            sqlite3.connect(temporary_path) as destination_connection,
        ):
            source_connection.execute("PRAGMA busy_timeout = 5000")
            source_connection.backup(destination_connection)
            result = destination_connection.execute("PRAGMA integrity_check").fetchone()
            if result != ("ok",):
                raise RuntimeError(
                    f"SQLite backup integrity check failed for {source}: {result!r}"
                )
        os.chmod(temporary_path, source.stat().st_mode & 0o7777)
        with temporary_path.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary_path, destination)
        try:
            directory_descriptor = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except OSError:
            pass
    finally:
        temporary_path.unlink(missing_ok=True)


def _promote_database_group(
    source_dir: Path,
    state_dir: Path,
    archive_dir: Path,
    report: dict[str, Any],
    *,
    replace_existing: bool,
) -> None:
    source = source_dir / "aios.db"
    destination = state_dir / "aios.db"
    if not source.is_file():
        for name in _DATABASE_FILES[1:]:
            _archive_path(
                source_dir / name,
                archive_dir / name,
                report,
                reason="orphaned legacy database sidecar",
                canonical_destination=state_dir / name,
            )
        return

    if destination.exists() or destination.is_symlink():
        if not replace_existing:
            _archive_database_group(
                source_dir,
                archive_dir,
                report,
                canonical_state_dir=state_dir,
            )
            return
        _archive_database_group(
            state_dir,
            archive_dir,
            report,
            canonical_state_dir=state_dir,
        )

    for name in _DATABASE_FILES[1:]:
        _archive_path(
            state_dir / name,
            archive_dir / name,
            report,
            reason="orphaned canonical database sidecar",
            canonical_destination=state_dir / name,
        )

    action = _plan_action(
        report,
        "promoted-database-snapshot",
        source,
        destination,
    )
    _backup_sqlite_database(source, destination)
    _complete_action(report, action)
    _archive_database_group(
        source_dir,
        archive_dir,
        report,
        canonical_state_dir=state_dir,
    )


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
        "cron_logs": "runs/cron_logs",
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

    for name in _DATABASE_FILES:
        _archive_path(
            workspace_dir / name,
            archive_dir / name,
            report,
            reason="orphaned database file after promotion",
            canonical_destination=data_dir / "state" / name,
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


def _canonical_deployment_source_path(
    source_value: str,
    *,
    workspace_dir: Path,
    source_root: Path,
    data_dir: Path,
) -> Path | None:
    """Translate a project source that lived in a legacy workspace tree."""

    raw_path = Path(source_value).expanduser()
    relative_path: Path | None = None

    if raw_path.is_absolute():
        resolved_path = raw_path.resolve(strict=False)
        for legacy_root, prefix in (
            (workspace_dir, Path()),
            (source_root / "session", Path("session")),
        ):
            try:
                suffix = resolved_path.relative_to(legacy_root.resolve(strict=False))
            except ValueError:
                continue
            relative_path = prefix / suffix
            break
    else:
        parts = raw_path.parts
        if parts[:1] == ("workspace",):
            parts = parts[1:]
        if parts[:1] and parts[0] in {"apps", "projects", "session", "sessions"}:
            relative_path = Path(*parts)

    if relative_path is None:
        return None

    parts = relative_path.parts
    if parts[:1] in (("apps",), ("projects",)) and len(parts) > 1:
        return data_dir / "projects" / Path(*parts[1:])

    if parts[:1] not in (("session",), ("sessions",)) or len(parts) < 2:
        return None

    chat_id = parts[1]
    category = parts[2] if len(parts) > 2 else None
    suffix = parts[3:] if category is not None else ()
    if category in {"files", "scratch"}:
        return data_dir / "sessions" / chat_id / "scratch" / Path(*suffix)
    if category == "uploads":
        return data_dir / "uploads" / chat_id / Path(*suffix)
    if category == "artifacts":
        return data_dir / "artifacts" / chat_id / Path(*suffix)
    return data_dir / "sessions" / chat_id / Path(*parts[2:])


def _promote_deployed_scratch_source(
    source: Path,
    *,
    slug: str,
    data_dir: Path,
    archive_root: Path,
    report: dict[str, Any],
) -> Path:
    """Promote a durable local deployment out of chat scratch storage."""

    resolved_source = source.resolve(strict=False)
    try:
        relative_source = resolved_source.relative_to(data_dir.resolve(strict=False))
    except ValueError:
        return source
    parts = relative_source.parts
    if len(parts) < 4 or parts[0] != "sessions" or parts[2] != "scratch":
        return source

    recorded_target: Path | None = None
    for action in reversed(report["actions"]):
        if not isinstance(action, dict):
            continue
        if action.get("action") != "moved" or action.get("source") != str(source):
            continue
        destination = action.get("destination")
        if not isinstance(destination, str):
            continue
        candidate = Path(destination)
        try:
            candidate.resolve(strict=False).relative_to(
                (data_dir / "projects").resolve(strict=False)
            )
        except ValueError:
            continue
        recorded_target = candidate
        break

    safe_slug = "".join(
        character if character.isalnum() or character in "._-" else "-"
        for character in slug
    ).strip("._-")
    if not safe_slug:
        safe_slug = "legacy-project"
    target = recorded_target or data_dir / "projects" / safe_slug
    if recorded_target is None and target.exists() and not _paths_are_equal(source, target):
        source_digest = hashlib.sha256(str(source).encode("utf-8")).hexdigest()[:8]
        target = data_dir / "projects" / f"{safe_slug}-legacy-{source_digest}"

    _merge_path(
        source,
        target,
        archive_root / "deployed-scratch" / safe_slug,
        report,
    )
    return target if target.exists() else source


def _rewrite_deployment_registry_paths(
    registry_path: Path,
    *,
    workspace_dir: Path,
    source_root: Path,
    data_dir: Path,
    archive_root: Path,
    report: dict[str, Any],
) -> None:
    """Keep deployed project records usable after their source trees move."""

    if not registry_path.is_file():
        return
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(registry, dict):
        return

    changed = False
    planned_actions: list[dict[str, object]] = []
    for slug, raw_project in registry.items():
        if not isinstance(raw_project, dict):
            continue
        source_value = raw_project.get("source_dir")
        if not isinstance(source_value, str):
            continue
        canonical_path = _canonical_deployment_source_path(
            source_value,
            workspace_dir=workspace_dir,
            source_root=source_root,
            data_dir=data_dir,
        )
        if canonical_path is None:
            candidate = Path(source_value).expanduser()
            if candidate.is_absolute():
                canonical_path = candidate
        if canonical_path is not None:
            canonical_path = _promote_deployed_scratch_source(
                canonical_path,
                slug=str(slug),
                data_dir=data_dir,
                archive_root=archive_root,
                report=report,
            )
        if canonical_path is None or str(canonical_path) == source_value:
            continue
        raw_project["source_dir"] = str(canonical_path)
        changed = True
        planned_actions.append(
            _plan_action(
                report,
                "rewrote-deployment-source",
                Path(source_value),
                canonical_path,
                project=str(slug),
                registry=str(registry_path),
            )
        )

    if not changed:
        return

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{registry_path.name}.",
        dir=str(registry_path.parent),
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(registry, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, registry_path)
        for action in planned_actions:
            _complete_action(report, action)
    finally:
        Path(temporary_name).unlink(missing_ok=True)


def _repair_completed_layout(
    report: dict[str, Any],
    *,
    report_path: Path,
    canonical_root: Path,
    source_root: Path,
    production_mode: bool,
) -> dict[str, Any]:
    """Apply safe finalizers added after the original v1 migration shipped."""

    initial_action_count = len(report["actions"])
    archive_root = canonical_root / "legacy" / _MIGRATION_NAME
    workspace_dir = (
        canonical_root / "workspace"
        if production_mode
        else source_root / "workspace"
    )
    report["_reportPath"] = str(report_path)
    _rewrite_deployment_registry_paths(
        canonical_root / "deployments" / "projects.json",
        workspace_dir=workspace_dir,
        source_root=source_root,
        data_dir=canonical_root,
        archive_root=archive_root,
        report=report,
    )
    _merge_path(
        canonical_root / "cron_logs",
        canonical_root / "runs" / "cron_logs",
        archive_root / "canonical-cron-logs",
        report,
    )
    if not production_mode:
        for source_name in ("uploads", "artifacts"):
            _merge_path(
                source_root / source_name,
                canonical_root / source_name,
                archive_root / f"root-{source_name}",
                report,
            )
    for directory in _layout_directories(canonical_root):
        directory.mkdir(parents=True, exist_ok=True)

    report.pop("_reportPath", None)
    if len(report["actions"]) != initial_action_count:
        report["repairedAt"] = datetime.now(timezone.utc).isoformat()
        _write_report(report_path, report)
    return report


def _write_report(report_path: Path, report: dict[str, Any]) -> None:
    persisted_report = {
        key: value for key, value in report.items() if not key.startswith("_")
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{_MIGRATION_NAME}.",
        dir=str(report_path.parent),
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(persisted_report, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, report_path)
    finally:
        Path(temporary_name).unlink(missing_ok=True)


@contextmanager
def _migration_file_lock(lock_path: Path):
    """Serialize storage migration across server/updater processes."""

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _load_migration_report(
    report_path: Path,
    *,
    canonical_root: Path,
) -> dict[str, Any] | None:
    if not report_path.exists():
        return None
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Storage migration report is unreadable: {report_path}") from exc
    if not isinstance(report, dict):
        raise RuntimeError(f"Storage migration report is invalid: {report_path}")

    expected = {
        "version": _STORAGE_LAYOUT_VERSION,
        "migration": _MIGRATION_NAME,
        "dataRoot": str(canonical_root),
    }
    mismatches = [key for key, value in expected.items() if report.get(key) != value]
    if mismatches:
        raise RuntimeError(
            "Storage migration report does not match this data root: "
            + ", ".join(mismatches)
        )
    if report.get("status") not in {"in_progress", "complete"}:
        raise RuntimeError(
            f"Storage migration report has an invalid status: {report.get('status')!r}"
        )
    if not isinstance(report.get("actions"), list):
        raise RuntimeError("Storage migration report has an invalid actions list")
    return report


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
    lock_path = report_path.with_suffix(".lock")

    canonical_root.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    with _MIGRATION_LOCK, _migration_file_lock(lock_path):
        existing = _load_migration_report(
            report_path,
            canonical_root=canonical_root,
        )
        if existing is not None and existing.get("status") == "complete":
            return _repair_completed_layout(
                existing,
                report_path=report_path,
                canonical_root=canonical_root,
                source_root=source_root,
                production_mode=production_mode,
            )

        archive_root = canonical_root / "legacy" / _MIGRATION_NAME
        if existing is None:
            report: dict[str, Any] = {
                "version": _STORAGE_LAYOUT_VERSION,
                "migration": _MIGRATION_NAME,
                "dataRoot": str(canonical_root),
                "startedAt": datetime.now(timezone.utc).isoformat(),
                "status": "in_progress",
                "actions": [],
            }
        else:
            report = existing
            resumed_at = report.setdefault("resumedAt", [])
            if not isinstance(resumed_at, list):
                raise RuntimeError("Storage migration report has an invalid resumedAt list")
            resumed_at.append(datetime.now(timezone.utc).isoformat())
        report["_reportPath"] = str(report_path)
        _write_report(report_path, report)

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
        _rewrite_deployment_registry_paths(
            canonical_root / "deployments" / "projects.json",
            workspace_dir=workspace_dir,
            source_root=source_root,
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
                ("cron_logs", "runs/cron_logs"),
                ("uploads", "uploads"),
                ("artifacts", "artifacts"),
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
        report.pop("_reportPath", None)
        _write_report(report_path, report)
        return report


def ensure_data_dir() -> Path:
    data_dir = get_data_dir()
    configured = _configured_data_dir()
    if configured is None:
        migrate_storage_layout(data_dir=data_dir)
    else:
        # An override selects an isolated root; migrate legacy children inside
        # that root without ever scanning or moving checkout-local data.
        migrate_storage_layout(
            data_dir=data_dir,
            project_root=data_dir,
            production=True,
        )
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
    "cron_logs": "runs/cron_logs",
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
        raw_path = Path(translated, *parts[1:])

    data_dir = ensure_data_dir().resolve(strict=False)
    resolved_path = (data_dir / raw_path).resolve(strict=False)
    try:
        resolved_path.relative_to(data_dir)
    except ValueError as exc:
        raise ValueError(
            "data-relative paths cannot escape the Mini AIOS data root"
        ) from exc
    return resolved_path
