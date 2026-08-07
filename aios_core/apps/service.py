from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import stat
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..skill_limits import MAX_SKILL_FILE_BYTES
from ..workspace import get_runtime_paths
from .manifest import (
    MANIFEST_FILENAME,
    ManifestValidationError,
    load_manifest,
    referenced_paths,
    validate_slug,
)
from .models import AppOrigin, AppRecord, Snapshot, ValidatedApp
from .registry import AppConflictError, AppRegistry

IGNORED_NAMES = frozenset(
    {
        ".DS_Store",
        ".git",
        ".hg",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".svn",
        ".tox",
        ".venv",
        "__pycache__",
        "coverage",
        "dist",
        "node_modules",
        "target",
        "venv",
    }
)
IGNORED_SUFFIXES = (".pyc", ".pyo")
MAX_SNAPSHOT_FILES = 10_000
MAX_SNAPSHOT_BYTES = 100 * 1024 * 1024
MAX_RETAINED_VERSIONS = 2

_LIFECYCLE_LOCKS: dict[str, threading.RLock] = {}
_LIFECYCLE_LOCKS_GUARD = threading.Lock()


class AppSourceError(ValueError):
    """Raised when an editable App tree violates the source contract."""


class SnapshotError(RuntimeError):
    """Raised when a content-addressed snapshot cannot be created safely."""


@dataclass(frozen=True)
class _SourceFile:
    relative_path: str
    path: Path
    size: int
    executable: bool


@dataclass(frozen=True)
class _SourceTree:
    root: Path
    files: tuple[_SourceFile, ...]
    directories: tuple[str, ...]

    @property
    def size_bytes(self) -> int:
        return sum(file.size for file in self.files)


class AppService:
    """Coordinates App source validation, snapshots, and registry lifecycle.

    This service never executes App code. Callers perform preparation in a
    runtime, then explicitly call ``mark_prepared`` and ``activate``.
    """

    def __init__(
        self,
        *,
        registry: AppRegistry | None = None,
        applications_dir: str | Path | None = None,
        state_dir: str | Path | None = None,
        db_path: str | Path | None = None,
    ) -> None:
        paths = get_runtime_paths()
        self.applications_dir = Path(
            applications_dir or paths.applications
        ).expanduser()
        self.state_dir = Path(state_dir or paths.state).expanduser()
        self.registry = registry or AppRegistry(db_path or paths.database)

    @property
    def snapshots_dir(self) -> Path:
        return self.state_dir / "apps" / "snapshots"

    @property
    def runtimes_dir(self) -> Path:
        return self.state_dir / "apps" / "runtime"

    def create_app(
        self,
        slug: str,
        *,
        name: str | None = None,
        description: str = "",
        version: str = "0.1.0",
        origin: AppOrigin | str = AppOrigin.AGENT,
        created_by_chat_id: str | None = None,
        created_by_run_id: str | None = None,
    ) -> AppRecord:
        slug = validate_slug(slug)
        source = self.applications_dir / slug
        self.applications_dir.mkdir(parents=True, exist_ok=True)
        if source.exists() or self.registry.get(slug) is not None:
            raise AppConflictError(f"an App named '{slug}' already exists")
        source.mkdir()
        try:
            manifest = {
                "schemaVersion": 1,
                "name": name or slug,
                "description": description,
                "version": version,
                "skills": [],
                "mcpServers": [],
                "executables": [],
                "prepare": [],
                "runtime": {
                    "network": False,
                    "persistentData": False,
                    "memoryMb": 512,
                    "cpus": 1.0,
                    "maxProcesses": 64,
                },
            }
            (source / MANIFEST_FILENAME).write_text(
                json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            return self.registry.register(
                slug,
                origin=origin,
                name=name or slug,
                description=description,
                version=version,
                created_by_chat_id=created_by_chat_id,
                created_by_run_id=created_by_run_id,
            )
        except Exception:
            self._remove_tree(source)
            raise

    def register_app(
        self,
        slug: str,
        *,
        origin: AppOrigin | str = AppOrigin.USER,
        created_by_chat_id: str | None = None,
        created_by_run_id: str | None = None,
    ) -> AppRecord:
        source = self._resolve_source(slug)
        if not source.is_dir():
            raise AppSourceError(f"App source directory does not exist: {source}")
        return self.registry.register(
            slug,
            origin=origin,
            created_by_chat_id=created_by_chat_id,
            created_by_run_id=created_by_run_id,
        )

    def validate(self, app_id_or_slug: str) -> ValidatedApp:
        app = self.registry.require(app_id_or_slug)
        with self._lifecycle_lock(app.id):
            app = self.registry.require(app.id)
            try:
                source = self.editable_path(app)
                tree = self._scan_tree(source)
                manifest_file = next(
                    (
                        file
                        for file in tree.files
                        if file.relative_path == MANIFEST_FILENAME
                    ),
                    None,
                )
                if manifest_file is None:
                    raise AppSourceError(f"App must contain {MANIFEST_FILENAME}")
                manifest = load_manifest(manifest_file.path)
                self._validate_references(manifest, tree)
                content_hash = self._content_hash(tree)
                snapshot = self._create_snapshot(app, tree, content_hash)

                # The immutable snapshot is the lifecycle authority. Reparse
                # it to close the source-edit race between the first manifest
                # read and hashing/copying the source tree.
                snapshot_tree = self._scan_tree(
                    snapshot.path,
                    ignore_generated=False,
                )
                manifest = load_manifest(snapshot.path / MANIFEST_FILENAME)
                self._validate_references(manifest, snapshot_tree)
                updated = self.registry.record_validation(
                    app.id,
                    manifest,
                    content_hash,
                )
                self._prune_unreferenced_versions(updated)
                return ValidatedApp(
                    app=updated,
                    manifest=manifest,
                    snapshot=snapshot,
                )
            except (AppSourceError, ManifestValidationError, SnapshotError) as exc:
                self.registry.record_error(app.id, str(exc))
                self._prune_unreferenced_versions(app)
                raise

    def mark_prepared(
        self,
        app_id_or_slug: str,
        content_hash: str,
    ) -> AppRecord:
        app = self.registry.require(app_id_or_slug)
        with self._lifecycle_lock(app.id):
            updated = self.registry.mark_prepared(app.id, content_hash)
            self._prune_unreferenced_versions(updated)
            return updated

    def activate(
        self,
        app_id_or_slug: str,
        content_hash: str | None = None,
        *,
        enable: bool = False,
    ) -> AppRecord:
        app = self.registry.require(app_id_or_slug)
        with self._lifecycle_lock(app.id):
            updated = self.registry.activate(
                app.id,
                content_hash,
                enable=enable,
            )
            self._prune_unreferenced_versions(updated)
            return updated

    def set_enabled(self, app_id_or_slug: str, enabled: bool) -> AppRecord:
        return self.registry.set_enabled(app_id_or_slug, enabled)

    def set_network_approved(
        self,
        app_id_or_slug: str,
        approved: bool,
    ) -> AppRecord:
        return self.registry.set_network_approved(app_id_or_slug, approved)

    def editable_path(self, app: AppRecord | str) -> Path:
        record = self.registry.require(app) if isinstance(app, str) else app
        expected = f"applications/{record.slug}"
        if record.root_path != expected:
            raise AppSourceError(f"unsafe registered App root: {record.root_path}")
        return self._resolve_source(record.slug)

    def snapshot_path(
        self,
        app: AppRecord | str,
        content_hash: str | None = None,
    ) -> Path:
        record = self.registry.require(app) if isinstance(app, str) else app
        selected_hash = content_hash or record.active_hash or record.validated_hash
        if not selected_hash or not _is_sha256(selected_hash):
            raise SnapshotError("App does not have a valid snapshot hash")
        path = self.snapshots_dir / record.id / selected_hash / "app"
        if not path.is_dir():
            raise SnapshotError(f"App snapshot is missing: {selected_hash}")
        return path

    def verify_snapshot(
        self,
        app: AppRecord | str,
        content_hash: str | None = None,
    ) -> Snapshot:
        record = self.registry.require(app) if isinstance(app, str) else app
        selected_hash = content_hash or record.active_hash or record.validated_hash
        path = self.snapshot_path(record, selected_hash)
        return self._verify_existing_snapshot(path, str(selected_hash))

    def _resolve_source(self, slug: str) -> Path:
        slug = validate_slug(slug)
        applications = self.applications_dir.resolve()
        source = applications / slug
        try:
            if source.is_symlink():
                raise AppSourceError("App source root cannot be a symlink")
            resolved = source.resolve()
            resolved.relative_to(applications)
        except (OSError, RuntimeError, ValueError) as exc:
            if isinstance(exc, AppSourceError):
                raise
            raise AppSourceError(f"unsafe App source directory: {source}") from exc
        if resolved.parent != applications:
            raise AppSourceError("App must be a direct child of applications")
        return resolved

    def _scan_tree(
        self,
        root: Path,
        *,
        ignore_generated: bool = True,
    ) -> _SourceTree:
        if root.is_symlink() or not root.is_dir():
            raise AppSourceError(f"App source is not a directory: {root}")
        files: list[_SourceFile] = []
        directories: list[str] = []
        total_size = 0

        def visit(directory: Path, relative_directory: Path) -> None:
            nonlocal total_size
            try:
                entries = sorted(os.scandir(directory), key=lambda item: item.name)
            except OSError as exc:
                raise AppSourceError(f"could not read App source: {directory}") from exc
            for entry in entries:
                relative = relative_directory / entry.name
                relative_text = relative.as_posix()
                try:
                    metadata = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    raise AppSourceError(
                        f"could not inspect App source path: {relative_text}"
                    ) from exc
                mode = metadata.st_mode
                if stat.S_ISLNK(mode):
                    raise AppSourceError(f"symlinks are not allowed: {relative_text}")
                ignored = ignore_generated and (
                    entry.name in IGNORED_NAMES or entry.name.endswith(IGNORED_SUFFIXES)
                )
                if ignored:
                    continue
                if stat.S_ISDIR(mode):
                    directories.append(relative_text)
                    visit(Path(entry.path), relative)
                    continue
                if not stat.S_ISREG(mode):
                    raise AppSourceError(
                        f"special files are not allowed: {relative_text}"
                    )
                if metadata.st_nlink != 1:
                    raise AppSourceError(f"hardlinks are not allowed: {relative_text}")
                total_size += metadata.st_size
                if len(files) >= MAX_SNAPSHOT_FILES:
                    raise AppSourceError(
                        f"App cannot contain more than {MAX_SNAPSHOT_FILES} files"
                    )
                if total_size > MAX_SNAPSHOT_BYTES:
                    raise AppSourceError(
                        f"App cannot exceed {MAX_SNAPSHOT_BYTES} bytes"
                    )
                files.append(
                    _SourceFile(
                        relative_path=relative_text,
                        path=Path(entry.path),
                        size=metadata.st_size,
                        executable=bool(mode & 0o111),
                    )
                )

        visit(root, Path())
        return _SourceTree(
            root=root,
            files=tuple(files),
            directories=tuple(sorted(directories)),
        )

    @staticmethod
    def _validate_references(manifest, tree: _SourceTree) -> None:
        source_files = {file.relative_path: file for file in tree.files}
        files = set(source_files)
        directories = {".", *tree.directories}
        for path, expected_type in referenced_paths(manifest):
            if expected_type == "file" and path not in files:
                raise ManifestValidationError(
                    f"manifest path must reference a regular file: {path}"
                )
            if expected_type == "directory" and path not in directories:
                raise ManifestValidationError(
                    f"manifest path must reference a directory: {path}"
                )
        for skill in manifest.skills:
            source_file = source_files[skill.path]
            if source_file.size > MAX_SKILL_FILE_BYTES:
                raise ManifestValidationError(
                    f"skill {skill.id} cannot exceed {MAX_SKILL_FILE_BYTES} bytes"
                )
            try:
                with source_file.path.open("rb") as file:
                    contents = file.read(MAX_SKILL_FILE_BYTES + 1)
                contents.decode("utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise ManifestValidationError(
                    f"skill {skill.id} must be readable UTF-8 text"
                ) from exc
            if len(contents) > MAX_SKILL_FILE_BYTES:
                raise ManifestValidationError(
                    f"skill {skill.id} cannot exceed {MAX_SKILL_FILE_BYTES} bytes"
                )

    @staticmethod
    def _content_hash(tree: _SourceTree) -> str:
        digest = hashlib.sha256()
        digest.update(b"aios-app-snapshot-v1\0")
        for file in tree.files:
            path_bytes = file.relative_path.encode("utf-8")
            digest.update(len(path_bytes).to_bytes(4, "big"))
            digest.update(path_bytes)
            digest.update(file.size.to_bytes(8, "big"))
            digest.update(b"x" if file.executable else b"-")
            copied = 0
            try:
                with file.path.open("rb") as source:
                    while chunk := source.read(1024 * 1024):
                        digest.update(chunk)
                        copied += len(chunk)
                metadata = file.path.lstat()
            except OSError as exc:
                raise AppSourceError(
                    f"could not hash App source path: {file.relative_path}"
                ) from exc
            if (
                copied != file.size
                or metadata.st_size != file.size
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or bool(metadata.st_mode & 0o111) != file.executable
            ):
                raise AppSourceError(
                    f"App source changed during validation: {file.relative_path}"
                )
        return digest.hexdigest()

    def _create_snapshot(
        self,
        app: AppRecord,
        tree: _SourceTree,
        content_hash: str,
    ) -> Snapshot:
        target = self.snapshots_dir / app.id / content_hash
        target_app = target / "app"
        if target.exists():
            return self._verify_existing_snapshot(target_app, content_hash)

        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".snapshot-", dir=str(target.parent)))
        temporary_app = temporary / "app"
        temporary_app.mkdir()
        try:
            for relative_directory in tree.directories:
                (temporary_app / relative_directory).mkdir(parents=True, exist_ok=True)
            for file in tree.files:
                destination = temporary_app / file.relative_path
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(file.path, destination, follow_symlinks=False)
                destination.chmod(0o555 if file.executable else 0o444)

            source_after_copy = self._scan_tree(tree.root)
            if self._content_hash(source_after_copy) != content_hash:
                raise AppSourceError("App source changed while its snapshot was copied")
            copied_tree = self._scan_tree(temporary_app, ignore_generated=False)
            if self._content_hash(copied_tree) != content_hash:
                raise SnapshotError(
                    "copied App snapshot did not match its content hash"
                )
            for directory in sorted(
                (path for path in temporary_app.rglob("*") if path.is_dir()),
                key=lambda path: len(path.parts),
                reverse=True,
            ):
                directory.chmod(0o555)
            temporary_app.chmod(0o555)
            try:
                temporary.rename(target)
            except OSError:
                if not target_app.is_dir():
                    raise
            return self._verify_existing_snapshot(target_app, content_hash)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)

    def _verify_existing_snapshot(
        self,
        snapshot_app: Path,
        expected_hash: str,
    ) -> Snapshot:
        if not snapshot_app.is_dir():
            raise SnapshotError(f"incomplete App snapshot: {expected_hash}")
        tree = self._scan_tree(snapshot_app, ignore_generated=False)
        actual_hash = self._content_hash(tree)
        if actual_hash != expected_hash:
            raise SnapshotError(
                f"App snapshot hash mismatch: expected {expected_hash}, got {actual_hash}"
            )
        return Snapshot(
            content_hash=expected_hash,
            path=snapshot_app,
            file_count=len(tree.files),
            size_bytes=tree.size_bytes,
        )

    def _prune_unreferenced_versions(self, app: AppRecord) -> None:
        app = self.registry.require(app.id)
        retained = {
            value
            for value in (
                app.validated_hash,
                app.prepared_hash,
                app.active_hash,
            )
            if value and _is_sha256(value)
        }
        snapshot_root = self.snapshots_dir / app.id
        try:
            recent_candidates = sorted(
                (
                    child
                    for child in snapshot_root.iterdir()
                    if _is_sha256(child.name) and child.name not in retained
                ),
                key=lambda child: (child.stat().st_mtime_ns, child.name),
                reverse=True,
            )
        except (FileNotFoundError, OSError):
            recent_candidates = []
        history_budget = max(0, MAX_RETAINED_VERSIONS - len(retained))
        retained.update(child.name for child in recent_candidates[:history_budget])
        for root in (
            snapshot_root,
            self.runtimes_dir / app.id,
        ):
            try:
                children = tuple(root.iterdir())
            except FileNotFoundError:
                continue
            except OSError:
                continue
            for child in children:
                if not _is_sha256(child.name) or child.name in retained:
                    continue
                try:
                    live = self.registry.require(app.id)
                    live_references = {
                        live.validated_hash,
                        live.prepared_hash,
                        live.active_hash,
                    }
                    if child.name in live_references:
                        continue
                    if child.is_symlink():
                        child.unlink()
                    elif child.is_dir():
                        self._remove_tree(child)
                except OSError:
                    # Cleanup is best effort and must never invalidate a
                    # successful registry lifecycle transaction.
                    continue

    @contextmanager
    def _lifecycle_lock(self, app_id: str):
        lock_key = f"{self.state_dir.resolve()}\0{app_id}"
        with _LIFECYCLE_LOCKS_GUARD:
            thread_lock = _LIFECYCLE_LOCKS.setdefault(
                lock_key,
                threading.RLock(),
            )
        with thread_lock:
            lock_dir = self.state_dir / "apps" / "locks"
            lock_dir.mkdir(parents=True, exist_ok=True)
            lock_name = hashlib.sha256(app_id.encode()).hexdigest() + ".lock"
            with (lock_dir / lock_name).open("a+") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _remove_tree(path: Path) -> None:
        for directory, child_directories, _files in os.walk(
            path,
            topdown=True,
            followlinks=False,
        ):
            Path(directory).chmod(0o700)
            for child in child_directories:
                child_path = Path(directory) / child
                if not child_path.is_symlink():
                    child_path.chmod(0o700)
        shutil.rmtree(path)


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )
