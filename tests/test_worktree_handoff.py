from __future__ import annotations

import subprocess
import tarfile
from pathlib import Path

import pytest

from aios_core.deploy.handoff_artifacts import (
    create_uploaded_artifact_from_handoff,
)
from aios_core.deploy.worktree_handoff import (
    WorktreeHandoffError,
    WorktreeRegistry,
    WorktreeStatus,
)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _app(apps_root: Path) -> tuple[Path, str, str]:
    app = apps_root / "app_123"
    app.mkdir(parents=True)
    _git(app, "init", "-b", "main")
    _git(app, "config", "user.name", "AIOS Test")
    _git(app, "config", "user.email", "aios@example.test")
    (app / "frontend").mkdir()
    (app / "frontend" / "index.html").write_text("<button>Green</button>\n")
    (app / "aios.deploy.yaml").write_text(
        "version: 1\napp_id: app_123\nfrontend:\n  source: frontend\n"
    )
    (app / "HISTORY.md").write_text("Button changed to green.\n")
    handoff = app / ".aios" / "handoffs" / "job-1.json"
    handoff.parent.mkdir(parents=True)
    handoff.write_text('{"job_id":"job-1"}\n')
    _git(app, "add", ".")
    _git(app, "commit", "-m", "feat: green button")
    return app, _git(app, "rev-parse", "HEAD"), _git(app, "rev-parse", "HEAD^{tree}")


class _Cloud:
    def __init__(self) -> None:
        self.members: list[str] = []

    def upload_artifact(self, archive) -> str:
        with tarfile.open(archive.path, "r:gz") as tar:
            self.members = tar.getnames()
        return "art_test123"


def test_codex_path_is_claimed_sealed_and_removed(tmp_path: Path) -> None:
    apps_root = tmp_path / "workspace" / "apps"
    app, commit, tree = _app(apps_root)
    registry = WorktreeRegistry(
        tmp_path / "workspace" / ".aios" / "worktrees",
        apps_root=apps_root,
    )
    reserved = registry.reserve(
        app_id="app_123",
        repository=app,
        owner_job_id="job-1",
        purpose="deploy_selected_commit",
        display_name="green-button",
    )
    _git(app, "worktree", "add", "--detach", reserved.path, commit)
    handoff = registry.publish_handoff(
        reserved.worktree_id,
        source_commit=commit,
        source_tree=tree,
        selection_reason="Changed the primary button to green",
    )

    marker = Path(handoff.path) / ".aios" / "WORKSPACE.md"
    assert "green-button" in marker.read_text()
    assert str(app) in marker.read_text()
    cloud = _Cloud()

    receipt = create_uploaded_artifact_from_handoff(
        registry=registry,
        cloud=cloud,  # type: ignore[arg-type]
        handoff_id=handoff.handoff_id,
    )

    assert receipt.artifact_id == "art_test123"
    assert receipt.source_commit == commit
    assert receipt.source_tree == tree
    assert receipt.cleanup_status == WorktreeStatus.REMOVED
    assert not Path(handoff.path).exists()
    assert registry.get(handoff.worktree_id).status == WorktreeStatus.REMOVED
    assert "frontend/index.html" in cloud.members
    assert "HISTORY.md" not in cloud.members
    assert not any(member.startswith(".aios/") for member in cloud.members)
    assert (app / "HISTORY.md").is_file()


def test_app_lease_is_exclusive_and_durable_across_registry_instances(
    tmp_path: Path,
) -> None:
    apps_root = tmp_path / "workspace" / "apps"
    app, _, _ = _app(apps_root)
    registry_root = tmp_path / "workspace" / ".aios" / "worktrees"
    first = WorktreeRegistry(registry_root, apps_root=apps_root)
    second = WorktreeRegistry(registry_root, apps_root=apps_root)

    acquired = first.acquire_app_lease(
        app_id="app_123", repository=app, owner_job_id="job-1"
    )

    assert second.get_app_lease("app_123") == acquired
    assert second.acquire_app_lease(
        app_id="app_123", repository=app, owner_job_id="job-1"
    ) == acquired
    with pytest.raises(Exception, match="Another Codex job"):
        second.acquire_app_lease(
            app_id="app_123", repository=app, owner_job_id="job-2"
        )
    with pytest.raises(Exception, match="different Codex job"):
        second.release_app_lease(app_id="app_123", owner_job_id="job-2")

    assert first.release_app_lease(app_id="app_123", owner_job_id="job-1") is True
    replacement = second.acquire_app_lease(
        app_id="app_123", repository=app, owner_job_id="job-2"
    )
    assert replacement.owner_job_id == "job-2"


def test_app_lease_reconciler_removes_only_non_active_owners(tmp_path: Path) -> None:
    apps_root = tmp_path / "workspace" / "apps"
    app, _, _ = _app(apps_root)
    registry = WorktreeRegistry(
        tmp_path / "workspace" / ".aios" / "worktrees",
        apps_root=apps_root,
    )
    registry.acquire_app_lease(
        app_id="app_123", repository=app, owner_job_id="job-active"
    )

    assert registry.reconcile_app_leases(
        active_codex_job_ids={"job-active"}
    ) == []
    released = registry.reconcile_app_leases(active_codex_job_ids=set())

    assert [lease.owner_job_id for lease in released] == ["job-active"]
    assert registry.get_app_lease("app_123") is None


def test_publishing_the_same_ready_handoff_is_idempotent(tmp_path: Path) -> None:
    apps_root = tmp_path / "workspace" / "apps"
    app, commit, tree = _app(apps_root)
    registry = WorktreeRegistry(
        tmp_path / "workspace" / ".aios" / "worktrees",
        apps_root=apps_root,
    )
    reserved = registry.reserve(
        app_id="app_123",
        repository=app,
        owner_job_id="job-1",
        purpose="deploy_selected_commit",
    )
    _git(app, "worktree", "add", "--detach", reserved.path, commit)
    first = registry.publish_handoff(
        reserved.worktree_id,
        source_commit=commit,
        source_tree=tree,
        selection_reason="Selected the green button version",
    )
    transition_count = len(first.transitions)

    second = registry.publish_handoff(
        reserved.worktree_id,
        source_commit=commit,
        source_tree=tree,
        selection_reason="Selected the green button version",
    )

    assert second == first
    assert len(second.transitions) == transition_count
    with pytest.raises(WorktreeHandoffError, match="source identity"):
        registry.publish_handoff(
            reserved.worktree_id,
            source_commit=commit,
            source_tree=tree,
            selection_reason="A different selection",
        )


def test_reconciler_removes_only_stale_registered_handoff(tmp_path: Path) -> None:
    apps_root = tmp_path / "workspace" / "apps"
    app, commit, tree = _app(apps_root)
    registry = WorktreeRegistry(
        tmp_path / "workspace" / ".aios" / "worktrees",
        apps_root=apps_root,
    )
    reserved = registry.reserve(
        app_id="app_123",
        repository=app,
        owner_job_id="abandoned-job",
        purpose="deploy_selected_commit",
    )
    _git(app, "worktree", "add", "--detach", reserved.path, commit)
    registry.publish_handoff(
        reserved.worktree_id,
        source_commit=commit,
        source_tree=tree,
    )

    reconciled = registry.reconcile_abandoned(older_than_seconds=0)

    assert reconciled[0].status == WorktreeStatus.REMOVED
    assert not Path(reserved.path).exists()
    assert registry.get(reserved.worktree_id).status == WorktreeStatus.REMOVED


def test_artifact_upload_failure_still_removes_worktree(tmp_path: Path) -> None:
    apps_root = tmp_path / "workspace" / "apps"
    app, commit, tree = _app(apps_root)
    registry = WorktreeRegistry(
        tmp_path / "workspace" / ".aios" / "worktrees",
        apps_root=apps_root,
    )
    reserved = registry.reserve(
        app_id="app_123",
        repository=app,
        owner_job_id="job-fail",
        purpose="deploy_selected_commit",
    )
    _git(app, "worktree", "add", "--detach", reserved.path, commit)
    handoff = registry.publish_handoff(
        reserved.worktree_id,
        source_commit=commit,
        source_tree=tree,
    )

    class FailingCloud:
        def upload_artifact(self, archive):
            raise RuntimeError("upload failed")

    with pytest.raises(Exception, match="upload failed"):
        create_uploaded_artifact_from_handoff(
            registry=registry,
            cloud=FailingCloud(),  # type: ignore[arg-type]
            handoff_id=handoff.handoff_id,
        )

    assert registry.get(reserved.worktree_id).status == WorktreeStatus.REMOVED
    assert not Path(reserved.path).exists()
