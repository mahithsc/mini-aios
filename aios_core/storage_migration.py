from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .workspace import RuntimePaths, get_runtime_paths

_MIGRATION_VERSION = 2
_WORKSPACE_DIRS = ("applications", "uploads", "downloads")
_LEGACY_ROOT_NAMES = (
    "aios.db",
    "aios.db-shm",
    "aios.db-wal",
    "crons.db",
    "crons.db-shm",
    "crons.db-wal",
    "session",
    "session_manifest.json",
    "uploads",
    "downloads",
    "runs",
    "cron_logs",
    "heartbeat_logs",
    "assistants",
)
_OLD_MIGRATION_NAMES = (
    "storage-layout-v1.json",
    "storage-layout-v1.staged",
)


@dataclass
class StorageMigrationReport:
    version: int = _MIGRATION_VERSION
    already_migrated: bool = False
    deleted_files: int = 0
    deleted_directories: int = 0
    deleted_bytes: int = 0
    copied_skill_files: int = 0
    preserved_chats: int = 0
    preserved_crons: int = 0


def _same_file_contents(left: Path, right: Path) -> bool:
    try:
        if left.stat().st_size != right.stat().st_size:
            return False
    except OSError:
        return False

    with left.open("rb") as left_handle, right.open("rb") as right_handle:
        while True:
            left_chunk = left_handle.read(1024 * 1024)
            right_chunk = right_handle.read(1024 * 1024)
            if left_chunk != right_chunk:
                return False
            if not left_chunk:
                return True


def _numbered_path(path: Path, number: int) -> Path:
    if path.suffix:
        return path.with_name(f"{path.stem} {number}{path.suffix}")
    return path.with_name(f"{path.name} {number}")


def _copy_file(source: Path, requested_destination: Path) -> Path:
    requested_destination.parent.mkdir(parents=True, exist_ok=True)
    destination = requested_destination
    number = 2
    while destination.exists():
        if destination.is_file() and _same_file_contents(source, destination):
            return destination
        destination = _numbered_path(requested_destination, number)
        number += 1

    fd, temporary_name = tempfile.mkstemp(
        prefix=".aios-skill-migrate.",
        dir=str(destination.parent),
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _copy_tree(
    source: Path,
    destination: Path,
    *,
    skip_relative_paths: set[str] | None = None,
) -> int:
    if not source.exists() or not source.is_dir():
        return 0

    copied = 0
    skipped = skip_relative_paths or set()
    for source_file in sorted(source.rglob("*")):
        if not source_file.is_file() or source_file.name == ".DS_Store":
            continue
        relative = source_file.relative_to(source)
        if relative.as_posix() in skipped:
            continue
        _copy_file(source_file, destination / relative)
        copied += 1
    return copied


def _load_skill_index(path: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("skills", []), list):
        return None
    return payload


def _skill_entry_identities(entry: object) -> set[tuple[str, str]]:
    if isinstance(entry, str):
        return {("path", entry)}
    if not isinstance(entry, dict):
        return {("value", json.dumps(entry, sort_keys=True, default=str))}

    identities = {
        (key, str(entry[key]))
        for key in ("id", "name", "file", "path")
        if entry.get(key)
    }
    if identities:
        return identities
    return {("value", json.dumps(entry, sort_keys=True, default=str))}


def _skill_index_version(payload: dict[str, object]) -> int:
    try:
        return int(payload.get("version", 1))
    except (TypeError, ValueError):
        return 1


def _write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=".aios-json.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        Path(temporary_name).unlink(missing_ok=True)


def _merge_skill_index(source: Path, destination: Path) -> int:
    if not source.exists():
        return 0
    source_payload = _load_skill_index(source)
    if source_payload is None:
        _copy_file(source, destination.with_name("skills_index_legacy.json"))
        return 1
    if not destination.exists():
        _copy_file(source, destination)
        return 1
    if _same_file_contents(source, destination):
        return 0

    destination_payload = _load_skill_index(destination)
    if destination_payload is None:
        _copy_file(
            destination,
            destination.with_name("skills_index_pre_migration.json"),
        )
        _write_json_atomic(destination, source_payload)
        return 1

    merged_entries = list(source_payload.get("skills", []))
    source_identities: set[tuple[str, str]] = set()
    for entry in merged_entries:
        source_identities.update(_skill_entry_identities(entry))
    for entry in destination_payload.get("skills", []):
        identities = _skill_entry_identities(entry)
        if identities.isdisjoint(source_identities):
            merged_entries.append(entry)
            source_identities.update(identities)

    merged_payload = {
        **destination_payload,
        **source_payload,
        "version": max(
            _skill_index_version(destination_payload),
            _skill_index_version(source_payload),
        ),
        "skills": merged_entries,
    }
    _copy_file(
        destination,
        destination.with_name("skills_index_pre_migration.json"),
    )
    _write_json_atomic(destination, merged_payload)
    return 1


def _migrate_skill_root(source: Path, destination: Path) -> int:
    if not source.exists() or source.resolve() == destination.resolve():
        return 0
    copied = _copy_tree(
        source,
        destination,
        skip_relative_paths={
            "README.md",
            "_template/SKILL.md",
            "skills_index.json",
        },
    )
    copied += _merge_skill_index(
        source / "skills_index.json",
        destination / "skills_index.json",
    )
    return copied


def _path_usage(path: Path) -> tuple[int, int, int]:
    if path.is_symlink() or path.is_file():
        try:
            return 1, 0, path.lstat().st_size
        except OSError:
            return 1, 0, 0
    if not path.exists():
        return 0, 0, 0

    files = 0
    directories = 1
    size_bytes = 0
    for root, directory_names, file_names in os.walk(path, followlinks=False):
        directories += len(directory_names)
        for name in file_names:
            files += 1
            try:
                size_bytes += (Path(root) / name).lstat().st_size
            except OSError:
                pass
    return files, directories, size_bytes


def _delete_entry(
    path: Path,
    *,
    expected_parent: Path,
    report: StorageMigrationReport,
) -> None:
    if path.parent.resolve() != expected_parent.resolve():
        raise RuntimeError(f"refusing to delete unexpected migration path: {path}")
    if not path.exists() and not path.is_symlink():
        return

    files, directories, size_bytes = _path_usage(path)
    if path.is_symlink() or path.is_file():
        path.unlink()
    else:
        shutil.rmtree(path)
    report.deleted_files += files
    report.deleted_directories += directories
    report.deleted_bytes += size_bytes


def _clear_directory(
    directory: Path,
    *,
    expected_parent: Path,
    report: StorageMigrationReport,
) -> None:
    if directory.parent.resolve() != expected_parent.resolve():
        raise RuntimeError(f"refusing to clear unexpected migration path: {directory}")
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        _delete_entry(
            directory,
            expected_parent=expected_parent,
            report=report,
        )
    directory.mkdir(parents=True, exist_ok=True)
    for entry in list(directory.iterdir()):
        _delete_entry(entry, expected_parent=directory, report=report)


def _database_count(database: Path, table: str) -> int:
    if not database.exists():
        return 0
    database_uri = f"file:{database.resolve()}?mode=ro"
    with sqlite3.connect(database_uri, uri=True, timeout=5.0) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if table not in tables:
            return 0
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _validate_preserved_database(paths: RuntimePaths) -> tuple[int, int]:
    if not paths.database.exists():
        raise RuntimeError(
            f"refusing to purge legacy storage before the active database exists: "
            f"{paths.database}"
        )
    database_uri = f"file:{paths.database.resolve()}?mode=ro"
    with sqlite3.connect(database_uri, uri=True, timeout=5.0) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
    if integrity is None or integrity[0] != "ok":
        raise RuntimeError(
            f"refusing to purge legacy storage because {paths.database} "
            "failed its integrity check"
        )
    return (
        _database_count(paths.database, "chats"),
        _database_count(paths.database, "crons"),
    )


def _write_report(
    path: Path,
    report: StorageMigrationReport,
    paths: RuntimePaths,
) -> None:
    payload = {
        **asdict(report),
        "database": str(paths.database),
        "skills": str(paths.skills),
        "completed_at": int(time.time() * 1000),
    }
    _write_json_atomic(path, payload)


def migrate_legacy_storage(
    *,
    paths: RuntimePaths | None = None,
    project_root: Path | None = None,
) -> StorageMigrationReport:
    """Permanently remove the retired per-chat storage layout.

    The active SQLite database and external skills root are the only legacy
    data carried forward. The shared applications directory is preserved,
    except for folders created by the previous recovery-style migration.
    Uploads and downloads are reset once during this destructive cutover.
    """
    del project_root  # Retained for compatibility with existing callers/tests.
    paths = paths or get_runtime_paths()
    paths.state.mkdir(parents=True, exist_ok=True)
    paths.skills.mkdir(parents=True, exist_ok=True)
    marker = paths.state / f"storage-layout-v{_MIGRATION_VERSION}.json"

    if marker.exists():
        for directory in (
            paths.applications,
            paths.uploads,
            paths.downloads,
            paths.runs,
            paths.cron_logs,
            paths.heartbeat_logs,
            paths.assistants,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        return StorageMigrationReport(already_migrated=True)

    report = StorageMigrationReport()
    report.preserved_chats, report.preserved_crons = _validate_preserved_database(
        paths
    )

    # Skills are the sole filesystem data preserved from the retired layout.
    for source in (
        paths.root / "skills",
        paths.workspace / "skills",
        paths.state / "legacy_workspace" / "skills",
    ):
        report.copied_skill_files += _migrate_skill_root(source, paths.skills)

    paths.workspace.mkdir(parents=True, exist_ok=True)

    # Keep current shared applications, but discard anything recovered from the
    # retired per-chat layout. Uploads/downloads are reset for a clean cutover.
    applications = paths.applications
    if applications.is_symlink() or (
        applications.exists() and not applications.is_dir()
    ):
        _delete_entry(
            applications,
            expected_parent=paths.workspace,
            report=report,
        )
    applications.mkdir(parents=True, exist_ok=True)
    for recovered_name in ("recovered", "recovered_workspace"):
        _delete_entry(
            applications / recovered_name,
            expected_parent=applications,
            report=report,
        )
    _clear_directory(
        paths.uploads,
        expected_parent=paths.workspace,
        report=report,
    )
    _clear_directory(
        paths.downloads,
        expected_parent=paths.workspace,
        report=report,
    )

    # A valid workspace contains exactly the three public roots.
    allowed_workspace_names = set(_WORKSPACE_DIRS)
    for entry in list(paths.workspace.iterdir()):
        if entry.name in allowed_workspace_names:
            continue
        _delete_entry(
            entry,
            expected_parent=paths.workspace,
            report=report,
        )

    # Development and older production layouts stored runtime data directly
    # under AIOS_HOME.
    for name in _LEGACY_ROOT_NAMES:
        _delete_entry(
            paths.root / name,
            expected_parent=paths.root,
            report=report,
        )

    # Old operational history is intentionally not carried forward.
    for entry in (
        paths.runs,
        paths.logs,
        paths.assistants,
        paths.state / "legacy_workspace",
    ):
        _delete_entry(
            entry,
            expected_parent=paths.state,
            report=report,
        )
    for name in _OLD_MIGRATION_NAMES:
        _delete_entry(
            paths.state / name,
            expected_parent=paths.state,
            report=report,
        )

    for directory in (
        paths.applications,
        paths.uploads,
        paths.downloads,
        paths.runs,
        paths.cron_logs,
        paths.heartbeat_logs,
        paths.assistants,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    _write_report(marker, report, paths)
    return report
