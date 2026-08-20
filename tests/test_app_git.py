from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from aios_core.app_git import (
    AppGitError,
    inspect_app_repository,
    validate_change_topology,
    validate_source_range,
)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _init(root: Path) -> None:
    root.mkdir(parents=True)
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "AIOS Test")
    _git(root, "config", "user.email", "aios@example.test")


def test_app_repository_must_be_rooted_exactly_at_app(tmp_path: Path) -> None:
    outer = tmp_path / "outer"
    _init(outer)
    app = outer / "workspace" / "apps" / "app_123"
    app.mkdir(parents=True)
    (outer / "tracked.txt").write_text("outer\n")
    _git(outer, "add", ".")
    _git(outer, "commit", "-m", "outer")

    with pytest.raises(AppGitError, match="not a Git repository"):
        inspect_app_repository(app)


def test_app_repository_rejects_dirty_state(tmp_path: Path) -> None:
    app = tmp_path / "app_123"
    _init(app)
    (app / "source.txt").write_text("clean\n")
    _git(app, "add", ".")
    _git(app, "commit", "-m", "baseline")
    (app / "untracked.txt").write_text("dirty\n")

    with pytest.raises(AppGitError, match="uncommitted changes"):
        inspect_app_repository(app)


def test_validates_exact_change_and_provenance_commits(tmp_path: Path) -> None:
    app = tmp_path / "app_123"
    _init(app)
    (app / "source.txt").write_text("blue\n")
    _git(app, "add", ".")
    _git(app, "commit", "-m", "baseline")
    base = _git(app, "rev-parse", "HEAD")

    (app / "source.txt").write_text("green\n")
    (app / "HISTORY.md").write_text("Changed the button to green.\n")
    _git(app, "add", ".")
    _git(app, "commit", "-m", "feat: green button")
    change = _git(app, "rev-parse", "HEAD")

    checkpoint = app / ".aios" / "checkpoints" / "job-1.json"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_text('{"change_commit":"' + change + '"}\n')
    with (app / "HISTORY.md").open("a") as history:
        history.write(f"Checkpoint: {change}\n")
    _git(app, "add", ".")
    _git(app, "commit", "-m", "chore(aios): record checkpoint")
    provenance = _git(app, "rev-parse", "HEAD")

    topology = validate_change_topology(
        app,
        base_commit=base,
        change_commit=change,
        provenance_commit=provenance,
        job_id="job-1",
    )

    assert topology.change_commit == change
    assert topology.provenance_files == (
        ".aios/checkpoints/job-1.json",
        "HISTORY.md",
    )


def test_validates_linear_multi_commit_source_range(tmp_path: Path) -> None:
    app = tmp_path / "app_123"
    _init(app)
    (app / "source.txt").write_text("blue\n")
    _git(app, "add", ".")
    _git(app, "commit", "-m", "baseline")
    base = _git(app, "rev-parse", "HEAD")

    (app / "source.txt").write_text("green\n")
    _git(app, "add", ".")
    _git(app, "commit", "-m", "change source")
    first = _git(app, "rev-parse", "HEAD")
    (app / "HISTORY.md").write_text("recorded\n")
    _git(app, "add", ".")
    _git(app, "commit", "-m", "record history")
    source = _git(app, "rev-parse", "HEAD")

    result = validate_source_range(app, base_commit=base, source_commit=source)

    assert result.commits == (first, source)
    assert result.changed_files == ("HISTORY.md", "source.txt")


def test_source_range_rejects_merge_commit(tmp_path: Path) -> None:
    app = tmp_path / "app_123"
    _init(app)
    (app / "source.txt").write_text("base\n")
    _git(app, "add", ".")
    _git(app, "commit", "-m", "baseline")
    base = _git(app, "rev-parse", "HEAD")
    _git(app, "checkout", "-b", "side")
    (app / "side.txt").write_text("side\n")
    _git(app, "add", ".")
    _git(app, "commit", "-m", "side")
    _git(app, "checkout", "main")
    (app / "main.txt").write_text("main\n")
    _git(app, "add", ".")
    _git(app, "commit", "-m", "main")
    _git(app, "merge", "--no-ff", "side", "-m", "merge")
    source = _git(app, "rev-parse", "HEAD")

    with pytest.raises(AppGitError, match="must (not contain merge commits|be linear)"):
        validate_source_range(app, base_commit=base, source_commit=source)
