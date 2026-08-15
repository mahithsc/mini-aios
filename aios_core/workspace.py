from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_PROD_ENV_VALUES = {"prod", "production"}
_WORKSPACE_ROOT_NAMES = {
    "applications": "applications",
    "uploads": "uploads",
    "downloads": "downloads",
}


class PathAccessError(ValueError):
    """Raised when an agent path crosses a runtime ownership boundary."""


@dataclass(frozen=True)
class RuntimePaths:
    root: Path
    state: Path
    skills: Path
    workspace: Path
    applications: Path
    uploads: Path
    downloads: Path
    runs: Path
    logs: Path
    cron_logs: Path
    heartbeat_logs: Path
    assistants: Path
    database: Path


def get_environment() -> str:
    return (
        os.getenv("AIOS_ENV")
        or os.getenv("APP_ENV")
        or os.getenv("ENV")
        or "dev"
    ).strip().lower()


def is_production() -> bool:
    return get_environment() in _PROD_ENV_VALUES


def _configured_path(name: str, default: Path) -> Path:
    value = os.getenv(name)
    return Path(value).expanduser() if value else default


def _paths_overlap(left: Path, right: Path) -> bool:
    left = left.resolve()
    right = right.resolve()
    try:
        left.relative_to(right)
        return True
    except ValueError:
        pass
    try:
        right.relative_to(left)
        return True
    except ValueError:
        return False


def get_runtime_paths() -> RuntimePaths:
    default_root = Path("~/.mini-aios").expanduser() if is_production() else _PROJECT_ROOT
    root = _configured_path("AIOS_HOME", default_root)
    state = _configured_path("AIOS_STATE_DIR", root / "state")
    skills = _configured_path("AIOS_SKILLS_DIR", root / "skills")
    workspace = _configured_path("AIOS_WORKSPACE_DIR", root / "workspace")
    if _paths_overlap(state, workspace):
        raise ValueError("state and workspace directories must not overlap")
    if _paths_overlap(skills, workspace):
        raise ValueError("skills and workspace directories must not overlap")
    applications = workspace / "applications"
    uploads = workspace / "uploads"
    downloads = workspace / "downloads"
    logs = state / "logs"
    return RuntimePaths(
        root=root,
        state=state,
        skills=skills,
        workspace=workspace,
        applications=applications,
        uploads=uploads,
        downloads=downloads,
        runs=state / "runs",
        logs=logs,
        cron_logs=logs / "crons",
        heartbeat_logs=logs / "heartbeat",
        assistants=state / "assistants",
        database=state / "aios.db",
    )


def get_project_root() -> Path:
    return _PROJECT_ROOT


def get_state_dir() -> Path:
    return get_runtime_paths().state


def get_skills_dir() -> Path:
    return get_runtime_paths().skills


def get_workspace_dir() -> Path:
    return get_runtime_paths().workspace


def get_applications_dir() -> Path:
    return get_runtime_paths().applications


def get_uploads_dir() -> Path:
    return get_runtime_paths().uploads


def get_downloads_dir() -> Path:
    return get_runtime_paths().downloads


def ensure_state_dir() -> Path:
    state_dir = get_state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def ensure_workspace_dir() -> Path:
    workspace_dir = get_workspace_dir()
    workspace_dir.mkdir(parents=True, exist_ok=True)
    return workspace_dir


def ensure_runtime_dirs() -> RuntimePaths:
    paths = get_runtime_paths()
    for directory in (
        paths.state,
        paths.skills,
        paths.workspace,
        paths.applications,
        paths.uploads,
        paths.downloads,
        paths.runs,
        paths.logs,
        paths.cron_logs,
        paths.heartbeat_logs,
        paths.assistants,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    return paths


def _resolved_within(path: Path, root: Path) -> Path | None:
    try:
        resolved = path.resolve()
        resolved.relative_to(root.resolve())
    except (ValueError, OSError, RuntimeError):
        return None
    return resolved


def _workspace_relative_candidate(raw_path: Path) -> Path:
    if not raw_path.parts:
        return get_workspace_dir()
    canonical = _WORKSPACE_ROOT_NAMES.get(raw_path.parts[0].lower())
    if canonical is None:
        return get_workspace_dir() / raw_path
    return get_workspace_dir() / canonical / Path(*raw_path.parts[1:])


def resolve_workspace_path(path: str | Path) -> Path:
    """Resolve a trusted workspace-relative path without allowing escapes."""
    raw_path = Path(path).expanduser()
    candidate = raw_path if raw_path.is_absolute() else _workspace_relative_candidate(raw_path)
    resolved = _resolved_within(candidate, get_workspace_dir())
    if resolved is None:
        raise PathAccessError(f"path is outside the workspace: {path}")
    return resolved


def resolve_agent_path(path: str | Path, *, for_write: bool = False) -> Path:
    """Resolve the small filesystem exposed to agents.

    Relative paths default to applications. The three workspace roots and the
    external skills root may also be addressed explicitly by name.
    """
    raw_path = Path(path).expanduser()
    paths = get_runtime_paths()

    if raw_path.is_absolute():
        candidate = raw_path
    elif raw_path.parts and raw_path.parts[0].lower() == "skills":
        candidate = paths.skills / Path(*raw_path.parts[1:])
    elif raw_path.parts and raw_path.parts[0].lower() in _WORKSPACE_ROOT_NAMES:
        canonical = _WORKSPACE_ROOT_NAMES[raw_path.parts[0].lower()]
        candidate = paths.workspace / canonical / Path(*raw_path.parts[1:])
    else:
        candidate = paths.applications / raw_path

    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError) as exc:
        raise PathAccessError(f"could not resolve agent path: {path}") from exc
    in_applications = _resolved_within(resolved, paths.applications) is not None
    in_uploads = _resolved_within(resolved, paths.uploads) is not None
    in_downloads = _resolved_within(resolved, paths.downloads) is not None
    in_skills = _resolved_within(resolved, paths.skills) is not None

    if for_write:
        if not in_applications:
            raise PathAccessError(
                "agents may only create or modify files inside applications"
            )
        return resolved

    if not any((in_applications, in_uploads, in_downloads, in_skills)):
        raise PathAccessError(
            "agents may only access applications, uploads, downloads, and skills"
        )
    return resolved


def default_agent_cwd() -> Path:
    applications = get_applications_dir()
    applications.mkdir(parents=True, exist_ok=True)
    return applications


def workspace_relative_path(path: str | Path) -> str:
    resolved = resolve_workspace_path(path)
    return resolved.relative_to(get_workspace_dir().resolve()).as_posix()


def legacy_runtime_roots() -> list[Path]:
    """Known pre-refactor roots, ordered from newest to oldest."""
    if is_production():
        return [Path("~/.mini-aios/workspace").expanduser()]
    return [_PROJECT_ROOT, _PROJECT_ROOT / "workspace"]
