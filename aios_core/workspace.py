from __future__ import annotations

import os
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_PROD_ENV_VALUES = {"prod", "production"}


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
    return _PROJECT_ROOT


def ensure_workspace_dir() -> Path:
    workspace_dir = get_workspace_dir()
    workspace_dir.mkdir(parents=True, exist_ok=True)
    return workspace_dir


def resolve_workspace_path(path: str | Path) -> Path:
    raw_path = Path(path).expanduser()
    if raw_path.is_absolute():
        return raw_path
    return ensure_workspace_dir() / raw_path
