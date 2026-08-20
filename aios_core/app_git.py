"""Strict Git invariants for durable AIOS application repositories.

Application repositories are intentionally nested beneath the mini-aios checkout.
Every operation in this module therefore verifies the app root itself instead of
accepting Git's normal ancestor discovery behavior.
"""

from __future__ import annotations

import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable


class AppGitError(RuntimeError):
    """An app repository does not satisfy the AIOS source-state contract."""


# Codex-job tests replace that module's ``subprocess.Popen`` with a protocol
# fake. Keep Git process creation isolated from that test seam (and from Codex
# app-server lifecycle patching in production embedders).
_GIT_POPEN = subprocess.Popen


@dataclass(frozen=True)
class AppRepositoryState:
    root: Path
    git_dir: Path
    common_dir: Path
    branch: str
    commit: str
    tree: str
    clean: bool
    status: str

    def model_dump(self) -> dict[str, Any]:
        payload = asdict(self)
        for key in ("root", "git_dir", "common_dir"):
            payload[key] = str(payload[key])
        return payload


@dataclass(frozen=True)
class ChangeTopology:
    base_commit: str
    change_commit: str
    provenance_commit: str
    change_files: tuple[str, ...]
    provenance_files: tuple[str, ...]


@dataclass(frozen=True)
class SourceRange:
    """A linear, non-merge source range owned by one app-change job."""

    base_commit: str
    source_commit: str
    commits: tuple[str, ...]
    changed_files: tuple[str, ...]


def run_git(
    repository: str | Path,
    arguments: Iterable[str],
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run Git with an argv-only interface and bounded, captured output."""

    root = Path(repository).resolve()
    try:
        process = _GIT_POPEN(
            ["git", "-C", str(root), *list(arguments)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout, stderr = process.communicate(timeout=30)
        result = subprocess.CompletedProcess(
            process.args,
            process.returncode,
            stdout,
            stderr,
        )
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.communicate()
        raise AppGitError(f"Git command timed out for {root}") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise AppGitError(f"Git command failed for {root}: {exc}") from exc
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise AppGitError(detail or f"Git exited {result.returncode} for {root}")
    return result


def inspect_app_repository(
    app_root: str | Path,
    *,
    require_clean: bool = True,
    require_branch: bool = True,
) -> AppRepositoryState:
    """Inspect a repository only when ``app_root`` is its exact top level."""

    root = Path(app_root).resolve()
    dot_git = root / ".git"
    if not root.is_dir() or not dot_git.exists():
        raise AppGitError(f"App root is not a Git repository: {root}")
    if dot_git.is_symlink():
        raise AppGitError(f"App root .git entry must not be a symbolic link: {root}")

    top_level = Path(
        run_git(root, ["rev-parse", "--show-toplevel"]).stdout.strip()
    ).resolve()
    if top_level != root:
        raise AppGitError(
            f"Git top level {top_level} does not equal canonical app root {root}"
        )

    git_dir = _resolve_git_path(root, "--git-dir")
    common_dir = _resolve_git_path(root, "--git-common-dir")
    _reject_in_progress_operations(root)
    _reject_unsupported_repository_features(root)

    branch_result = run_git(
        root, ["symbolic-ref", "--quiet", "--short", "HEAD"], check=False
    )
    branch = branch_result.stdout.strip()
    if require_branch and not branch:
        raise AppGitError("Canonical app repository must be on a named branch")

    commit = resolve_commit(root, "HEAD")
    tree = resolve_tree(root, commit)
    status = run_git(root, ["status", "--porcelain=v1", "--untracked-files=all"]).stdout
    clean = not status.strip()
    if require_clean and not clean:
        raise AppGitError(
            "Canonical app repository contains uncommitted changes:\n" + status.rstrip()
        )
    return AppRepositoryState(
        root=root,
        git_dir=git_dir,
        common_dir=common_dir,
        branch=branch,
        commit=commit,
        tree=tree,
        clean=clean,
        status=status,
    )


def resolve_commit(repository: str | Path, revision: str) -> str:
    if not isinstance(revision, str) or not revision.strip():
        raise AppGitError("A Git revision is required")
    result = run_git(
        repository,
        ["rev-parse", "--verify", "--end-of-options", f"{revision.strip()}^{{commit}}"],
    )
    value = result.stdout.strip()
    if not value:
        raise AppGitError(f"Git revision did not resolve to a commit: {revision}")
    return value


def resolve_tree(repository: str | Path, commit: str) -> str:
    result = run_git(
        repository,
        ["rev-parse", "--verify", "--end-of-options", f"{commit}^{{tree}}"],
    )
    value = result.stdout.strip()
    if not value:
        raise AppGitError(f"Could not resolve tree for commit {commit}")
    return value


def read_blob(repository: str | Path, commit: str, relative_path: str) -> bytes:
    """Read one committed blob without consulting mutable worktree bytes."""

    root = Path(repository).resolve()
    try:
        process = _GIT_POPEN(
            ["git", "-C", str(root), "show", f"{commit}:{relative_path}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, stderr = process.communicate(timeout=30)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.communicate()
        raise AppGitError(f"Timed out reading Git blob {relative_path}") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise AppGitError(f"Could not read Git blob {relative_path}: {exc}") from exc
    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        raise AppGitError(detail or f"Could not read Git blob {relative_path}")
    return stdout


def validate_change_topology(
    repository: str | Path,
    *,
    base_commit: str,
    change_commit: str,
    provenance_commit: str,
    job_id: str,
) -> ChangeTopology:
    """Validate the exact single-parent ``B -> C -> M`` Codex contract."""

    root = Path(repository).resolve()
    base = resolve_commit(root, base_commit)
    change = resolve_commit(root, change_commit)
    provenance = resolve_commit(root, provenance_commit)
    if _parents(root, change) != [base]:
        raise AppGitError(
            "Change commit C must have exactly the captured base B as parent"
        )
    if _parents(root, provenance) != [change]:
        raise AppGitError(
            "Provenance commit M must have exactly change commit C as parent"
        )

    change_files = tuple(_diff_names(root, base, change))
    provenance_files = tuple(_diff_names(root, change, provenance))
    checkpoint = f".aios/checkpoints/{job_id}.json"
    allowed = {"HISTORY.md", checkpoint}
    unexpected = sorted(set(provenance_files) - allowed)
    if unexpected:
        raise AppGitError(
            "Provenance commit M changed non-metadata files: " + ", ".join(unexpected)
        )
    if "HISTORY.md" not in provenance_files or checkpoint not in provenance_files:
        raise AppGitError(
            "Provenance commit M must update HISTORY.md and its exact checkpoint file"
        )
    return ChangeTopology(
        base_commit=base,
        change_commit=change,
        provenance_commit=provenance,
        change_files=change_files,
        provenance_files=provenance_files,
    )


def validate_source_range(
    repository: str | Path,
    *,
    base_commit: str,
    source_commit: str,
) -> SourceRange:
    """Validate a non-empty, first-parent-linear ``B..C`` source range.

    Contract v3 permits Codex to make more than one commit so interrupted work
    can resume without rewriting history. Merges and unrelated history remain
    forbidden, and the resulting range must change at least one tracked path.
    """

    root = Path(repository).resolve()
    base = resolve_commit(root, base_commit)
    source = resolve_commit(root, source_commit)
    if base == source:
        raise AppGitError("Source commit C must differ from captured base B")
    ancestor = run_git(
        root,
        ["merge-base", "--is-ancestor", base, source],
        check=False,
    )
    if ancestor.returncode != 0:
        raise AppGitError("Source commit C must descend from captured base B")

    lines = run_git(
        root,
        ["rev-list", "--reverse", "--parents", f"{base}..{source}"],
    ).stdout.splitlines()
    previous = base
    commits: list[str] = []
    for line in lines:
        fields = line.split()
        if len(fields) != 2:
            raise AppGitError("The B-to-C source range must not contain merge commits")
        commit, parent = fields
        if parent != previous:
            raise AppGitError("The B-to-C source range must be linear")
        commits.append(commit)
        previous = commit
    if not commits or commits[-1] != source:
        raise AppGitError("Could not resolve a complete linear B-to-C source range")

    changed_files = tuple(_diff_names(root, base, source))
    if not changed_files:
        raise AppGitError("The B-to-C source range does not change any files")
    return SourceRange(
        base_commit=base,
        source_commit=source,
        commits=tuple(commits),
        changed_files=changed_files,
    )


def registered_worktree(
    repository: str | Path, workspace_path: str | Path
) -> dict[str, str] | None:
    """Return Git's worktree record for one exact real path."""

    expected = Path(workspace_path).resolve()
    output = run_git(repository, ["worktree", "list", "--porcelain"]).stdout
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in output.splitlines():
        if not line:
            if current:
                records.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    if current:
        records.append(current)
    for record in records:
        raw = record.get("worktree")
        if raw and Path(raw).resolve() == expected:
            return record
    return None


def _resolve_git_path(root: Path, argument: str) -> Path:
    raw = run_git(root, ["rev-parse", argument]).stdout.strip()
    path = Path(raw)
    return (root / path).resolve() if not path.is_absolute() else path.resolve()


def _parents(root: Path, commit: str) -> list[str]:
    fields = run_git(root, ["rev-list", "--parents", "-n", "1", commit]).stdout.split()
    return fields[1:]


def _diff_names(root: Path, before: str, after: str) -> list[str]:
    output = run_git(
        root, ["diff", "--name-only", "--no-renames", "-z", before, after]
    ).stdout
    return sorted(path for path in output.split("\0") if path)


def list_changed_files(
    repository: str | Path, before_commit: str, after_commit: str
) -> list[str]:
    """Return the exact no-rename changed-file inventory between two commits."""

    root = Path(repository).resolve()
    before = resolve_commit(root, before_commit)
    after = resolve_commit(root, after_commit)
    return _diff_names(root, before, after)


def _reject_in_progress_operations(root: Path) -> None:
    markers = (
        "MERGE_HEAD",
        "CHERRY_PICK_HEAD",
        "REVERT_HEAD",
        "BISECT_LOG",
        "rebase-apply",
        "rebase-merge",
    )
    active: list[str] = []
    for marker in markers:
        raw_path = run_git(root, ["rev-parse", "--git-path", marker]).stdout.strip()
        path = Path(raw_path)
        if raw_path and (path if path.is_absolute() else root / path).exists():
            active.append(marker)
    if active:
        raise AppGitError("Git operation is in progress: " + ", ".join(active))


def _reject_unsupported_repository_features(root: Path) -> None:
    replacements = run_git(
        root, ["for-each-ref", "--format=%(refname)", "refs/replace"]
    )
    if replacements.stdout.strip():
        raise AppGitError("Git replace refs are not supported for app deployments")
    raw_grafts = run_git(
        root, ["rev-parse", "--git-path", "info/grafts"]
    ).stdout.strip()
    grafts_path = Path(raw_grafts)
    grafts = grafts_path if grafts_path.is_absolute() else root / grafts_path
    if grafts.is_file() and grafts.stat().st_size:
        raise AppGitError("Git grafts are not supported for app deployments")

    sparse = run_git(root, ["config", "--bool", "core.sparseCheckout"], check=False)
    if sparse.stdout.strip().lower() == "true":
        raise AppGitError("Sparse app checkouts are not supported for deployment")
    lfs = run_git(
        root,
        [
            "grep",
            "-l",
            "-F",
            "version https://git-lfs.github.com/spec/v1",
            "HEAD",
            "--",
        ],
        check=False,
    )
    if lfs.returncode == 0 and lfs.stdout.strip():
        raise AppGitError("Git LFS pointer files are not supported for deployment")

    staged = run_git(root, ["ls-files", "--stage", "-z"]).stdout
    for entry in staged.split("\0"):
        if not entry:
            continue
        mode = entry.split(" ", 1)[0]
        _, _, relative = entry.partition("\t")
        if mode == "160000":
            raise AppGitError("Git submodules are not supported for app deployments")
        if mode == "120000":
            raise AppGitError(
                "Tracked symbolic links are not supported for app deployments"
            )
        if relative.endswith(".gitattributes"):
            attributes = read_blob(root, "HEAD", relative).decode(
                "utf-8", errors="replace"
            )
            if any(
                token in attributes
                for token in ("filter=", "working-tree-encoding=", " ident")
            ):
                raise AppGitError(
                    "Git content filters/encodings are not supported for deployment"
                )
