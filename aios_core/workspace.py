from __future__ import annotations

import os
import shutil
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEV_WORKSPACE_DIR = _PROJECT_ROOT / "workspace"
_PROD_ENV_VALUES = {"prod", "production"}
_RUNTIME_DIR_NAMES = ("skills", "session", "runs", "apps", "cron_logs", "heartbeat_logs")
_RUNTIME_FILE_NAMES = ("aios.db", "crons.db")

def get_environment() -> str:
    return (
        os.getenv("AIOS_ENV")
        or os.getenv("APP_ENV")
        or os.getenv("ENV")
        or "dev"
    ).strip().lower()

def is_production() -> bool:
    return get_environment() in _PROD_ENV_VALUES

def get_workspace_dir() -> Path:
    if is_production():
        return Path("~/.mini-aios/workspace").expanduser()
    return _DEV_WORKSPACE_DIR


def _migrate_dev_workspace_contents(workspace_dir: Path) -> None:
    if is_production():
        return

    for name in _RUNTIME_DIR_NAMES:
        source = _PROJECT_ROOT / name
        target = workspace_dir / name
        if source.exists() and not target.exists():
            shutil.move(str(source), str(target))

    for name in _RUNTIME_FILE_NAMES:
        source = _PROJECT_ROOT / name
        target = workspace_dir / name
        if source.exists() and not target.exists():
            shutil.move(str(source), str(target))

def ensure_workspace_dir() -> Path:
    workspace_dir = get_workspace_dir()
    workspace_dir.mkdir(parents=True, exist_ok=True)
    _migrate_dev_workspace_contents(workspace_dir)
    return workspace_dir

def resolve_workspace_path(path: str | Path) -> Path:
    raw_path = Path(path).expanduser()
    if raw_path.is_absolute():
        return raw_path
    return ensure_workspace_dir() / raw_path