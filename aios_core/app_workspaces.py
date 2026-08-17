"""Durable local workspaces for cloud applications.

Chat sessions are ephemeral conversation sandboxes.  Application source must
outlive those sandboxes, so every cloud app has one canonical local root under
``workspace/apps/<app-id>``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from .workspace import resolve_workspace_path

APP_METADATA_NAME = ".aios-app.json"
APP_README_NAME = "README.md"
APPS_README_NAME = "README.md"
_APP_ID = re.compile(r"^app_[A-Za-z0-9]+$")
_IGNORED_COPY_PARTS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "node_modules",
}
_IGNORED_SCORE_PARTS = _IGNORED_COPY_PARTS | {"build", "dist"}


class AppWorkspaceError(RuntimeError):
    """A durable app workspace could not be created or resolved."""


def get_apps_dir(apps_dir: str | Path | None = None) -> Path:
    return Path(apps_dir) if apps_dir is not None else resolve_workspace_path("apps")


def get_app_workspace_dir(
    app_id: str, *, apps_dir: str | Path | None = None
) -> Path:
    _validate_app_id(app_id)
    return get_apps_dir(apps_dir) / app_id


def create_app_workspace(
    app_id: str,
    name: str,
    *,
    origin_chat_id: str | None = None,
    apps_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Create or refresh the canonical local root for a newly reserved app."""

    root = get_app_workspace_dir(app_id, apps_dir=apps_dir)
    created = not root.exists()
    root.mkdir(parents=True, exist_ok=True)
    _ensure_apps_readme(root.parent)
    metadata = _write_metadata(
        root,
        app_id=app_id,
        name=name,
        origin_chat_id=origin_chat_id,
    )
    _ensure_app_readme(root, metadata)
    return _workspace_payload(root, metadata, created=created, migrated_from=None)


def resolve_app_workspace(
    app_id: str,
    *,
    name: str | None = None,
    origin_chat_id: str | None = None,
    apps_dir: str | Path | None = None,
    session_dir: str | Path | None = None,
    adopt_legacy: bool = True,
) -> dict[str, Any]:
    """Resolve an app root, optionally adopting the best legacy session copy.

    Missing source is reported instead of creating an empty deployable project.
    That prevents Codex from fabricating a replacement app during redeployment.
    """

    root = get_app_workspace_dir(app_id, apps_dir=apps_dir)
    if _workspace_has_source(root):
        metadata = _write_metadata(
            root,
            app_id=app_id,
            name=name,
            origin_chat_id=origin_chat_id,
        )
        _ensure_apps_readme(root.parent)
        _ensure_app_readme(root, metadata)
        return _workspace_payload(root, metadata, created=False, migrated_from=None)

    if not adopt_legacy:
        return _missing_payload(root, app_id)

    candidates = find_legacy_app_workspaces(app_id, session_dir=session_dir)
    if not candidates:
        return _missing_payload(root, app_id)

    source = candidates[0]
    root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        source,
        root,
        dirs_exist_ok=True,
        symlinks=True,
        ignore=shutil.ignore_patterns(*sorted(_IGNORED_COPY_PARTS)),
    )
    _ensure_apps_readme(root.parent)
    metadata = _write_metadata(
        root,
        app_id=app_id,
        name=name,
        origin_chat_id=origin_chat_id or _legacy_chat_id(source, session_dir),
        migrated_from=str(source.resolve()),
    )
    _ensure_app_readme(root, metadata)
    return _workspace_payload(
        root,
        metadata,
        created=True,
        migrated_from=str(source.resolve()),
    )


def find_legacy_app_workspaces(
    app_id: str, *, session_dir: str | Path | None = None
) -> list[Path]:
    """Find session-scoped app roots and rank richer source trees first."""

    _validate_app_id(app_id)
    sessions = (
        Path(session_dir)
        if session_dir is not None
        else resolve_workspace_path("session")
    )
    if not sessions.is_dir():
        return []

    matches: list[Path] = []
    for files_root in sessions.glob("*/files"):
        if not files_root.is_dir():
            continue
        for current, directories, filenames in os.walk(files_root):
            directories[:] = [
                name for name in directories if name not in _IGNORED_COPY_PARTS
            ]
            if "aios.deploy.yaml" not in filenames:
                continue
            manifest_path = Path(current) / "aios.deploy.yaml"
            if _manifest_app_id(manifest_path) == app_id:
                matches.append(manifest_path.parent)
    return sorted(matches, key=_legacy_score, reverse=True)


def _validate_app_id(app_id: str) -> None:
    if not isinstance(app_id, str) or not _APP_ID.fullmatch(app_id):
        raise AppWorkspaceError("app_id must match app_<letters-or-digits>")


def _manifest_app_id(manifest_path: Path) -> str | None:
    try:
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        return None
    if not isinstance(raw, dict):
        return None
    value = raw.get("app_id")
    return value if isinstance(value, str) else None


def _legacy_score(root: Path) -> tuple[int, int, float]:
    source_files = 0
    total_files = 0
    newest_mtime = 0.0
    try:
        for path in root.rglob("*"):
            relative = path.relative_to(root)
            if any(part in _IGNORED_SCORE_PARTS for part in relative.parts):
                continue
            if not path.is_file():
                continue
            total_files += 1
            newest_mtime = max(newest_mtime, path.stat().st_mtime)
            if path.name not in {"aios.deploy.yaml", APP_METADATA_NAME}:
                source_files += 1
            if total_files >= 2_000:
                break
    except OSError:
        pass
    return source_files, total_files, newest_mtime


def _workspace_has_source(root: Path) -> bool:
    if not root.is_dir():
        return False
    try:
        return any(
            path.is_file()
            and path.name not in {APP_METADATA_NAME, APP_README_NAME}
            for path in root.iterdir()
        )
    except OSError:
        return False


def _legacy_chat_id(source: Path, session_dir: str | Path | None) -> str | None:
    sessions = (
        Path(session_dir).resolve()
        if session_dir is not None
        else resolve_workspace_path("session").resolve()
    )
    try:
        return source.resolve().relative_to(sessions).parts[0]
    except (ValueError, IndexError):
        return None


def _write_metadata(
    root: Path,
    *,
    app_id: str,
    name: str | None,
    origin_chat_id: str | None,
    migrated_from: str | None = None,
) -> dict[str, Any]:
    metadata_path = root / APP_METADATA_NAME
    existing: dict[str, Any] = {}
    if metadata_path.is_file():
        try:
            loaded = json.loads(metadata_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except (OSError, UnicodeError, json.JSONDecodeError):
            existing = {}

    now = datetime.now(UTC).isoformat()
    resolved_name = name or existing.get("name") or app_id
    if not isinstance(resolved_name, str) or not resolved_name.strip():
        resolved_name = app_id
    metadata: dict[str, Any] = {
        "version": 1,
        "app_id": app_id,
        "name": resolved_name.strip(),
        "created_at": existing.get("created_at") or now,
        "updated_at": now,
    }
    resolved_origin = origin_chat_id or existing.get("origin_chat_id")
    if resolved_origin:
        metadata["origin_chat_id"] = str(resolved_origin)
    resolved_migration = migrated_from or existing.get("migrated_from")
    if resolved_migration:
        metadata["migrated_from"] = str(resolved_migration)

    temporary = metadata_path.with_name(f"{metadata_path.name}.{uuid4().hex}.tmp")
    temporary.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    temporary.replace(metadata_path)
    return metadata


def _ensure_apps_readme(apps_dir: Path) -> None:
    apps_dir.mkdir(parents=True, exist_ok=True)
    path = apps_dir / APPS_README_NAME
    if path.exists():
        return
    path.write_text(
        "# AIOS Apps\n\n"
        "Each directory is the durable source-of-truth workspace for one cloud app.\n"
        "Chat session folders contain conversation-specific files, not canonical app code.\n",
        encoding="utf-8",
    )


def _ensure_app_readme(root: Path, metadata: dict[str, Any]) -> None:
    path = root / APP_README_NAME
    if path.exists():
        return
    path.write_text(
        f"# {metadata['name']}\n\n"
        f"- **AIOS app ID:** `{metadata['app_id']}`\n"
        "- **Source of truth:** this directory\n"
        "- **Deployment manifest:** `aios.deploy.yaml`\n\n"
        "## Development\n\n"
        "Keep application source and deployment configuration here. Chat sessions may "
        "reference this folder, but should not create separate project copies.\n",
        encoding="utf-8",
    )


def _workspace_payload(
    root: Path,
    metadata: dict[str, Any],
    *,
    created: bool,
    migrated_from: str | None,
) -> dict[str, Any]:
    return {
        "app_id": metadata["app_id"],
        "name": metadata["name"],
        "found": True,
        "created": created,
        "workspace_path": str(root.resolve()),
        "readme_path": str((root / APP_README_NAME).resolve()),
        "migrated_from": migrated_from,
    }


def _missing_payload(root: Path, app_id: str) -> dict[str, Any]:
    return {
        "app_id": app_id,
        "found": False,
        "workspace_path": str(root.resolve()),
        "error": (
            "No durable or legacy local source workspace was found for this app. "
            "Do not fabricate replacement source; restore/import the original project."
        ),
    }
