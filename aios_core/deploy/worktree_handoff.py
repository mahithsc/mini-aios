"""Registered Codex-to-deployment worktree handoffs.

Codex owns discovery and worktree preparation. Once it publishes a handoff, the
artifact builder atomically claims ownership, validates the checkout, seals an
artifact, and removes the disposable worktree.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from ..app_git import (
    AppGitError,
    inspect_app_repository,
    registered_worktree,
    resolve_commit,
    resolve_tree,
    run_git,
)
from ..workspace import get_workspace_dir

_APP_ID = re.compile(r"^app_[A-Za-z0-9]+$")
_WORKTREE_ID = re.compile(r"^wt_[a-f0-9]{16}$")


class WorktreeHandoffError(RuntimeError):
    """A worktree handoff is missing, invalid, or has the wrong owner/state."""


class AppLeaseRecord(BaseModel):
    """Durable exclusive ownership of one canonical app repository."""

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    app_id: str = Field(pattern=r"^app_[A-Za-z0-9]+$")
    repository: str
    owner_job_id: str
    acquired_at: str
    updated_at: str


class WorktreeStatus(StrEnum):
    RESERVED = "reserved"
    CODEX_ALLOCATING = "codex_allocating"
    HANDOFF_READY = "handoff_ready"
    ARTIFACT_CLAIMED = "artifact_claimed"
    VERIFYING = "verifying"
    SANITIZING = "sanitizing"
    SEALING = "sealing"
    SEALED = "sealed"
    CLEANUP_PENDING = "cleanup_pending"
    REMOVED = "removed"
    FAILED = "failed"
    CLEANUP_FAILED = "cleanup_failed"
    QUARANTINED = "quarantined"


class WorktreeTransition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: WorktreeStatus
    at: str
    owner: str
    error: str | None = None


class WorktreeRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    handoff_id: str
    worktree_id: str = Field(pattern=r"^wt_[a-f0-9]{16}$")
    display_name: str
    app_id: str = Field(pattern=r"^app_[A-Za-z0-9]+$")
    repository: str
    path: str
    source_commit: str | None = None
    source_tree: str | None = None
    provenance_commit: str | None = None
    selection_reason: str | None = None
    purpose: str
    owner_job_id: str
    owner: str
    status: WorktreeStatus
    created_at: str
    updated_at: str
    removed_at: str | None = None
    last_error: str | None = None
    transitions: list[WorktreeTransition]


class WorktreeRegistry:
    """Atomic JSON registry for AIOS-owned disposable Git worktrees."""

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        apps_root: str | Path | None = None,
    ) -> None:
        workspace = get_workspace_dir().resolve()
        self.root = (
            Path(root).resolve()
            if root is not None
            else (workspace / ".aios" / "worktrees").resolve()
        )
        self.apps_root = (
            Path(apps_root).resolve()
            if apps_root is not None
            else (workspace / "apps").resolve()
        )
        self.records_dir = self.root / "records"
        self.checkouts_dir = self.root / "checkouts"
        self.leases_dir = self.root / "leases"
        self.records_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.checkouts_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.leases_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._assert_safe_directory(self.root)
        self._assert_safe_directory(self.records_dir)
        self._assert_safe_directory(self.checkouts_dir)
        self._assert_safe_directory(self.leases_dir)
        self._lock_path = self.root / ".registry.lock"

    def acquire_app_lease(
        self,
        *,
        app_id: str,
        repository: str | Path,
        owner_job_id: str,
    ) -> AppLeaseRecord:
        """Atomically acquire the one durable Codex mutation lease for an app."""

        if not _APP_ID.fullmatch(app_id):
            raise WorktreeHandoffError("Invalid app_id for app lease")
        repo = Path(repository).resolve()
        expected_repo = (self.apps_root / app_id).resolve()
        if repo != expected_repo:
            raise WorktreeHandoffError(
                f"Repository {repo} is not the canonical app root {expected_repo}"
            )
        if not owner_job_id:
            raise WorktreeHandoffError("owner_job_id is required for app lease")
        with self._locked():
            existing = self._read_app_lease(app_id)
            if existing is not None:
                if (
                    existing.owner_job_id == owner_job_id
                    and Path(existing.repository).resolve() == repo
                ):
                    return existing
                raise WorktreeHandoffError(
                    "Another Codex job is already changing this app; "
                    f"active job: {existing.owner_job_id}"
                )
            now = _now()
            lease = AppLeaseRecord(
                app_id=app_id,
                repository=str(repo),
                owner_job_id=owner_job_id,
                acquired_at=now,
                updated_at=now,
            )
            self._write_app_lease(lease)
            return lease

    def get_app_lease(self, app_id: str) -> AppLeaseRecord | None:
        if not _APP_ID.fullmatch(app_id):
            raise WorktreeHandoffError("Invalid app_id for app lease")
        with self._locked():
            return self._read_app_lease(app_id)

    def release_app_lease(self, *, app_id: str, owner_job_id: str) -> bool:
        """Release only the exact job's lease; repeated release is harmless."""

        if not _APP_ID.fullmatch(app_id):
            raise WorktreeHandoffError("Invalid app_id for app lease")
        with self._locked():
            existing = self._read_app_lease(app_id)
            if existing is None:
                return False
            if existing.owner_job_id != owner_job_id:
                raise WorktreeHandoffError(
                    "App lease belongs to a different Codex job"
                )
            self._app_lease_path(app_id).unlink()
            return True

    def reconcile_app_leases(
        self, *, active_codex_job_ids: set[str]
    ) -> list[AppLeaseRecord]:
        """Remove leases whose durable Codex owner is no longer active."""

        released: list[AppLeaseRecord] = []
        with self._locked():
            for path in sorted(self.leases_dir.glob("app_*.json")):
                try:
                    lease = AppLeaseRecord.model_validate_json(
                        path.read_text(encoding="utf-8")
                    )
                except (OSError, UnicodeError, ValueError):
                    continue
                if lease.owner_job_id in active_codex_job_ids:
                    continue
                path.unlink()
                released.append(lease)
        return released

    def reserve(
        self,
        *,
        app_id: str,
        repository: str | Path,
        owner_job_id: str,
        purpose: str,
        display_name: str | None = None,
    ) -> WorktreeRecord:
        if not _APP_ID.fullmatch(app_id):
            raise WorktreeHandoffError("Invalid app_id for worktree reservation")
        repo = Path(repository).resolve()
        expected_repo = (self.apps_root / app_id).resolve()
        if repo != expected_repo:
            raise WorktreeHandoffError(
                f"Repository {repo} is not the canonical app root {expected_repo}"
            )
        if not owner_job_id or not purpose:
            raise WorktreeHandoffError("owner_job_id and purpose are required")
        worktree_id = f"wt_{uuid4().hex[:16]}"
        now = _now()
        record = WorktreeRecord(
            handoff_id=f"wh_{uuid4().hex[:16]}",
            worktree_id=worktree_id,
            display_name=(display_name or worktree_id).strip(),
            app_id=app_id,
            repository=str(repo),
            path=str((self.checkouts_dir / worktree_id).resolve()),
            purpose=purpose,
            owner_job_id=owner_job_id,
            owner=f"codex:{owner_job_id}",
            status=WorktreeStatus.CODEX_ALLOCATING,
            created_at=now,
            updated_at=now,
            transitions=[
                WorktreeTransition(
                    status=WorktreeStatus.CODEX_ALLOCATING,
                    at=now,
                    owner=f"codex:{owner_job_id}",
                )
            ],
        )
        with self._locked():
            self._write(record)
        return record

    def get(self, worktree_id: str) -> WorktreeRecord:
        self._validate_worktree_id(worktree_id)
        path = self.records_dir / f"{worktree_id}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return WorktreeRecord.model_validate(payload)
        except FileNotFoundError as exc:
            raise WorktreeHandoffError(f"Unknown worktree: {worktree_id}") from exc
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise WorktreeHandoffError(
                f"Invalid worktree registry record {worktree_id}: {exc}"
            ) from exc

    def get_handoff(self, handoff_id: str) -> WorktreeRecord:
        if not isinstance(handoff_id, str) or not handoff_id.startswith("wh_"):
            raise WorktreeHandoffError("Invalid handoff_id")
        for path in self.records_dir.glob("wt_*.json"):
            try:
                record = WorktreeRecord.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
            except (OSError, UnicodeError, ValueError):
                continue
            if record.handoff_id == handoff_id:
                return record
        raise WorktreeHandoffError(f"Unknown handoff: {handoff_id}")

    def publish_handoff(
        self,
        worktree_id: str,
        *,
        source_commit: str,
        source_tree: str | None = None,
        provenance_commit: str | None = None,
        selection_reason: str | None = None,
    ) -> WorktreeRecord:
        """Validate and publish a checkout, idempotently for the same identity."""

        with self._locked():
            record = self.get(worktree_id)
            if record.status == WorktreeStatus.HANDOFF_READY:
                commit, tree = self._validate_checkout(
                    record,
                    source_commit=source_commit,
                    source_tree=source_tree or record.source_tree,
                    allow_marker=True,
                )
                if any(
                    (
                        record.source_commit != commit,
                        record.source_tree != tree,
                        record.provenance_commit != provenance_commit,
                        record.selection_reason != selection_reason,
                    )
                ):
                    raise WorktreeHandoffError(
                        "Published handoff does not match the requested source identity"
                    )
                return record
            if record.status != WorktreeStatus.CODEX_ALLOCATING:
                raise WorktreeHandoffError(
                    f"Cannot publish worktree in state {record.status}"
                )
            descriptor = Path(record.path) / ".aios" / "CODEX_HANDOFF.json"
            if descriptor.parent.is_symlink() or descriptor.is_symlink():
                raise WorktreeHandoffError(
                    "Codex handoff descriptor must not be a symlink"
                )
            commit, tree = self._validate_checkout(
                record,
                source_commit=source_commit,
                source_tree=source_tree,
                allow_marker=False,
                allow_codex_handoff=True,
            )
            descriptor.unlink(missing_ok=True)
            commit, tree = self._validate_checkout(
                record,
                source_commit=source_commit,
                source_tree=source_tree,
                allow_marker=False,
            )
            self._write_workspace_marker(record, commit=commit, tree=tree)
            record.source_commit = commit
            record.source_tree = tree
            record.provenance_commit = provenance_commit
            record.selection_reason = selection_reason
            return self._transition_record(
                record,
                WorktreeStatus.HANDOFF_READY,
                owner=record.owner,
            )

    def claim_artifact(
        self,
        handoff_id: str,
        *,
        artifact_run_id: str,
    ) -> WorktreeRecord:
        """Atomically claim and revalidate an immutable workspace handoff."""

        with self._locked():
            record = self.get_handoff(handoff_id)
            if record.status != WorktreeStatus.HANDOFF_READY:
                raise WorktreeHandoffError(
                    f"Handoff is not ready; current state is {record.status}"
                )
            if not record.source_commit:
                raise WorktreeHandoffError("Ready handoff has no source commit")
            commit, tree = self._validate_checkout(
                record,
                source_commit=record.source_commit,
                source_tree=record.source_tree,
                allow_marker=True,
            )
            record.source_commit = commit
            record.source_tree = tree
            return self._transition_record(
                record,
                WorktreeStatus.ARTIFACT_CLAIMED,
                owner=f"artifact:{artifact_run_id}",
            )

    def transition(
        self,
        worktree_id: str,
        status: WorktreeStatus,
        *,
        owner: str,
        expected: set[WorktreeStatus],
        error: str | None = None,
    ) -> WorktreeRecord:
        with self._locked():
            record = self.get(worktree_id)
            if record.status not in expected:
                raise WorktreeHandoffError(
                    f"Cannot move {worktree_id} from {record.status} to {status}"
                )
            if record.owner != owner:
                raise WorktreeHandoffError("Worktree lifecycle owner does not match")
            return self._transition_record(record, status, owner=owner, error=error)

    def sanitize_claimed(self, record: WorktreeRecord) -> WorktreeRecord:
        """Restore an owned disposable checkout to the exact selected commit."""

        owner = record.owner
        record = self.transition(
            record.worktree_id,
            WorktreeStatus.SANITIZING,
            owner=owner,
            expected={WorktreeStatus.ARTIFACT_CLAIMED, WorktreeStatus.VERIFYING},
        )
        self._validate_checkout(
            record,
            source_commit=record.source_commit or "",
            source_tree=record.source_tree,
            allow_marker=True,
        )
        workspace = Path(record.path)
        run_git(workspace, ["reset", "--hard", record.source_commit or ""])
        run_git(workspace, ["clean", "-ffdx"])
        self._write_workspace_marker(
            record,
            commit=record.source_commit or "",
            tree=record.source_tree or "",
        )
        self._validate_checkout(
            record,
            source_commit=record.source_commit or "",
            source_tree=record.source_tree,
            allow_marker=True,
        )
        return record

    def validate_claimed(self, record: WorktreeRecord) -> tuple[str, str]:
        current = self.get(record.worktree_id)
        if current.owner != record.owner or current.status not in {
            WorktreeStatus.ARTIFACT_CLAIMED,
            WorktreeStatus.VERIFYING,
            WorktreeStatus.SANITIZING,
            WorktreeStatus.SEALING,
        }:
            raise WorktreeHandoffError("Artifact run no longer owns this worktree")
        return self._validate_checkout(
            current,
            source_commit=current.source_commit or "",
            source_tree=current.source_tree,
            allow_marker=True,
        )

    def cleanup(self, record: WorktreeRecord) -> WorktreeRecord:
        """Remove only the exact validated AIOS worktree and retain its record."""

        owner = record.owner
        current = self.get(record.worktree_id)
        if current.owner != owner:
            raise WorktreeHandoffError("Cannot clean a worktree owned by another run")
        if current.status == WorktreeStatus.REMOVED:
            return current
        current = self.transition(
            current.worktree_id,
            WorktreeStatus.CLEANUP_PENDING,
            owner=owner,
            expected={
                WorktreeStatus.ARTIFACT_CLAIMED,
                WorktreeStatus.VERIFYING,
                WorktreeStatus.SANITIZING,
                WorktreeStatus.SEALING,
                WorktreeStatus.SEALED,
                WorktreeStatus.FAILED,
                WorktreeStatus.CLEANUP_FAILED,
            },
        )
        try:
            self._validate_registry_path(current)
            registration = registered_worktree(current.repository, current.path)
            if registration is not None:
                run_git(
                    current.repository,
                    ["worktree", "remove", "--force", current.path],
                )
            if Path(current.path).exists():
                raise WorktreeHandoffError(
                    "Git removed the registration but the checkout path still exists"
                )
            if registered_worktree(current.repository, current.path) is not None:
                raise WorktreeHandoffError("Git still lists the removed worktree")
        except (AppGitError, OSError, WorktreeHandoffError) as exc:
            return self.transition(
                current.worktree_id,
                WorktreeStatus.CLEANUP_FAILED,
                owner=owner,
                expected={WorktreeStatus.CLEANUP_PENDING},
                error=str(exc),
            )
        removed = self.transition(
            current.worktree_id,
            WorktreeStatus.REMOVED,
            owner=owner,
            expected={WorktreeStatus.CLEANUP_PENDING},
        )
        removed.removed_at = removed.updated_at
        with self._locked():
            self._write(removed)
        return removed

    def abandon_codex_handoff(
        self, worktree_id: str, *, owner_job_id: str, error: str
    ) -> WorktreeRecord:
        """Reclaim a reservation after its Codex job errors or is cancelled."""

        record = self.get(worktree_id)
        expected_owner = f"codex:{owner_job_id}"
        if record.status == WorktreeStatus.REMOVED:
            return record
        if record.owner != expected_owner:
            raise WorktreeHandoffError("Codex no longer owns this worktree handoff")
        if record.status not in {WorktreeStatus.FAILED, WorktreeStatus.CLEANUP_FAILED}:
            record = self.transition(
                worktree_id,
                WorktreeStatus.FAILED,
                owner=expected_owner,
                expected={record.status},
                error=error,
            )
        return self.cleanup(record)

    def reconcile_abandoned(
        self,
        *,
        active_codex_job_ids: set[str] | None = None,
        older_than_seconds: float = 3600,
    ) -> list[WorktreeRecord]:
        """Clean stale AIOS-owned worktrees without pruning unrelated Git state."""

        active = active_codex_job_ids or set()
        cutoff = datetime.now(UTC) - timedelta(seconds=max(0.0, older_than_seconds))
        reconciled: list[WorktreeRecord] = []
        for path in sorted(self.records_dir.glob("wt_*.json")):
            try:
                record = self.get(path.stem)
                updated = datetime.fromisoformat(record.updated_at)
            except (ValueError, WorktreeHandoffError):
                continue
            if record.status in {
                WorktreeStatus.REMOVED,
                WorktreeStatus.QUARANTINED,
            }:
                continue
            if record.owner.startswith("codex:"):
                owner_job = record.owner.split(":", 1)[1]
                if owner_job in active:
                    continue
            if updated > cutoff:
                continue
            try:
                if record.status not in {
                    WorktreeStatus.FAILED,
                    WorktreeStatus.CLEANUP_FAILED,
                }:
                    record = self.transition(
                        record.worktree_id,
                        WorktreeStatus.FAILED,
                        owner=record.owner,
                        expected={record.status},
                        error="Abandoned worktree reclaimed by reconciler",
                    )
                record = self.cleanup(record)
            except WorktreeHandoffError as exc:
                try:
                    record = self.transition(
                        record.worktree_id,
                        WorktreeStatus.QUARANTINED,
                        owner=record.owner,
                        expected={record.status},
                        error=str(exc),
                    )
                except WorktreeHandoffError:
                    record = self.get(record.worktree_id)
            reconciled.append(record)
        return reconciled

    def _validate_checkout(
        self,
        record: WorktreeRecord,
        *,
        source_commit: str,
        source_tree: str | None,
        allow_marker: bool,
        allow_codex_handoff: bool = False,
    ) -> tuple[str, str]:
        self._validate_registry_path(record)
        registration = registered_worktree(record.repository, record.path)
        if registration is None:
            raise WorktreeHandoffError("Path is not registered as a Git worktree")
        if "detached" not in registration:
            raise WorktreeHandoffError("Deployment worktree must have detached HEAD")
        workspace = Path(record.path).resolve()
        state = inspect_app_repository(
            workspace,
            require_clean=False,
            require_branch=False,
        )
        canonical = inspect_app_repository(record.repository, require_clean=True)
        if state.common_dir != canonical.common_dir:
            raise WorktreeHandoffError("Worktree belongs to a different Git repository")
        commit = resolve_commit(workspace, "HEAD")
        expected_commit = resolve_commit(record.repository, source_commit)
        if commit != expected_commit:
            raise WorktreeHandoffError("Worktree HEAD does not match selected commit")
        tree = resolve_tree(workspace, commit)
        if source_tree and tree != source_tree:
            raise WorktreeHandoffError(
                "Worktree tree does not match selected source tree"
            )
        status_lines = [line for line in state.status.splitlines() if line]
        allowed = {"?? .aios/WORKSPACE.md"} if allow_marker else set()
        if allow_codex_handoff:
            allowed.add("?? .aios/CODEX_HANDOFF.json")
        unexpected = [line for line in status_lines if line not in allowed]
        if unexpected:
            raise WorktreeHandoffError(
                "Worktree contains files outside the selected commit:\n"
                + "\n".join(unexpected)
            )
        return commit, tree

    def _validate_registry_path(self, record: WorktreeRecord) -> None:
        self._validate_worktree_id(record.worktree_id)
        path = Path(record.path)
        if path.is_symlink():
            raise WorktreeHandoffError("Worktree path must not be a symbolic link")
        resolved = path.resolve()
        expected = (self.checkouts_dir / record.worktree_id).resolve()
        if resolved != expected or resolved.parent != self.checkouts_dir.resolve():
            raise WorktreeHandoffError("Worktree path escapes the AIOS checkout root")
        repository = Path(record.repository).resolve()
        expected_repo = (self.apps_root / record.app_id).resolve()
        if repository != expected_repo:
            raise WorktreeHandoffError(
                "Registered repository is not the canonical app root"
            )
        if resolved == repository:
            raise WorktreeHandoffError(
                "Disposable worktree cannot be the canonical checkout"
            )

    def _write_workspace_marker(
        self, record: WorktreeRecord, *, commit: str, tree: str
    ) -> None:
        workspace = Path(record.path).resolve()
        tracked = run_git(
            workspace,
            ["ls-files", "--error-unmatch", ".aios/WORKSPACE.md"],
            check=False,
        )
        if tracked.returncode == 0:
            raise WorktreeHandoffError(
                "Selected commit already tracks reserved .aios/WORKSPACE.md"
            )
        marker = workspace / ".aios" / "WORKSPACE.md"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            "# Disposable AIOS Worktree\n\n"
            f"- **Worktree:** `{record.display_name}` (`{record.worktree_id}`)\n"
            f"- **App:** `{record.app_id}`\n"
            f"- **Canonical repository:** `{record.repository}`\n"
            f"- **Workspace path:** `{record.path}`\n"
            f"- **Source commit:** `{commit}`\n"
            f"- **Source tree:** `{tree}`\n"
            f"- **Purpose:** `{record.purpose}`\n"
            f"- **Registry:** `{self.records_dir / (record.worktree_id + '.json')}`\n\n"
            "This is a disposable detached AIOS worktree. Do not make or commit "
            "source changes here. Apply source changes in the canonical repository "
            "through Codex. Verification and packaging outputs must not be committed. "
            "The registry is authoritative; this file is only a creation-time snapshot.\n",
            encoding="utf-8",
        )

    def _transition_record(
        self,
        record: WorktreeRecord,
        status: WorktreeStatus,
        *,
        owner: str,
        error: str | None = None,
    ) -> WorktreeRecord:
        now = _now()
        record.status = status
        record.owner = owner
        record.updated_at = now
        record.last_error = error
        record.transitions.append(
            WorktreeTransition(status=status, at=now, owner=owner, error=error)
        )
        self._write(record)
        return record

    def _write(self, record: WorktreeRecord) -> None:
        path = self.records_dir / f"{record.worktree_id}.json"
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(record.model_dump_json(indent=2))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        finally:
            temporary.unlink(missing_ok=True)

    def _app_lease_path(self, app_id: str) -> Path:
        return self.leases_dir / f"{app_id}.json"

    def _read_app_lease(self, app_id: str) -> AppLeaseRecord | None:
        path = self._app_lease_path(app_id)
        try:
            if path.is_symlink():
                raise WorktreeHandoffError(
                    f"App lease record must not be a symlink: {path}"
                )
            return AppLeaseRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError, ValueError) as exc:
            raise WorktreeHandoffError(
                f"Invalid app lease record for {app_id}: {exc}"
            ) from exc

    def _write_app_lease(self, lease: AppLeaseRecord) -> None:
        path = self._app_lease_path(lease.app_id)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(lease.model_dump_json(indent=2))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        finally:
            temporary.unlink(missing_ok=True)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self._lock_path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _assert_safe_directory(path: Path) -> None:
        if path.is_symlink():
            raise WorktreeHandoffError(f"Registry path must not be a symlink: {path}")

    @staticmethod
    def _validate_worktree_id(worktree_id: str) -> None:
        if not isinstance(worktree_id, str) or not _WORKTREE_ID.fullmatch(worktree_id):
            raise WorktreeHandoffError("Invalid worktree_id")


def _now() -> str:
    return datetime.now(UTC).isoformat()
