"""Durable local workspaces for cloud applications.

Chat scratch is ephemeral. Application source must outlive a conversation, so
every cloud app has one canonical local root under ``projects/<app-id>`` in the
AIOS data directory.
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

from .workspace import get_projects_dir, get_sessions_dir

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
_ROOT_METADATA_FILES = {APP_METADATA_NAME, APP_README_NAME, "aios.deploy.yaml"}


class AppWorkspaceError(RuntimeError):
    """A durable app workspace could not be created or resolved."""


def get_apps_dir(apps_dir: str | Path | None = None) -> Path:
    """Return the canonical projects root (legacy name retained for API stability)."""

    return Path(apps_dir) if apps_dir is not None else get_projects_dir()


def get_app_workspace_dir(app_id: str, *, apps_dir: str | Path | None = None) -> Path:
    _validate_app_id(app_id)
    root = get_apps_dir(apps_dir)
    workspace = root / app_id
    if workspace.is_symlink():
        raise AppWorkspaceError(f"app workspace cannot be a symbolic link: {workspace}")
    try:
        workspace.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise AppWorkspaceError(
            f"app workspace escapes the apps directory: {workspace}"
        ) from exc
    return workspace


def list_app_workspaces(*, apps_dir: str | Path | None = None) -> dict[str, Any]:
    """List every durable local app directory, including unfinished apps.

    This is deliberately a local, read-only inventory. A workspace does not
    need to be registered in the cloud or have a complete deployment manifest
    to appear here.
    """

    root = get_apps_dir(apps_dir)
    apps: list[dict[str, Any]] = []
    if root.is_dir():
        try:
            directories = [
                path
                for path in root.iterdir()
                if path.is_dir() and not path.is_symlink()
            ]
        except OSError as exc:
            raise AppWorkspaceError(f"could not list app workspaces: {exc}") from exc
        for workspace in directories:
            apps.append(_local_workspace_summary(workspace))

    apps.sort(key=lambda app: (str(app["name"]).casefold(), app["app_id"]))
    return {"apps_dir": str(root.resolve()), "apps": apps}


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
    _ensure_workspace_identity(root, app_id)
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
    That prevents a coding agent from fabricating a replacement app during
    redeployment.
    """

    root = get_app_workspace_dir(app_id, apps_dir=apps_dir)
    _ensure_workspace_identity(root, app_id)
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
    sessions = Path(session_dir) if session_dir is not None else get_sessions_dir()
    if not sessions.is_dir():
        return []
    sessions_root = sessions.resolve()

    matches: list[Path] = []
    scratch_roots = [
        *sessions.glob("*/scratch"),
        *sessions.glob("*/files"),
    ]
    for files_root in scratch_roots:
        if (
            not files_root.is_dir()
            or files_root.is_symlink()
            or files_root.parent.is_symlink()
        ):
            continue
        try:
            files_root.resolve().relative_to(sessions_root)
        except ValueError:
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


def _local_workspace_summary(root: Path) -> dict[str, Any]:
    metadata = _read_mapping(root / APP_METADATA_NAME, json_format=True)
    manifest = _read_mapping(root / "aios.deploy.yaml", json_format=False)

    metadata_app_id = _nonempty_string(metadata.get("app_id"))
    manifest_app_id = _nonempty_string(manifest.get("app_id"))
    if _APP_ID.fullmatch(root.name):
        app_id = root.name
    else:
        app_id = metadata_app_id or manifest_app_id or root.name

    name = _nonempty_string(metadata.get("name"))
    if name is None:
        name = _read_readme_title(root / APP_README_NAME)
    if name is None:
        package = _read_mapping(root / "package.json", json_format=True)
        name = _nonempty_string(package.get("name"))
    if name is None:
        name = root.name

    components = [
        component
        for component in ("database", "server", "frontend")
        if manifest.get(component) is not None
    ]
    summary = {
        "id": app_id,
        "app_id": app_id,
        "name": name,
        "workspace_path": str(root.resolve()),
        "has_metadata": (root / APP_METADATA_NAME).is_file(),
        "has_manifest": (root / "aios.deploy.yaml").is_file(),
        "has_source": _workspace_has_source(root),
        "components": components,
    }
    conflicting_ids = sorted(
        {
            value
            for value in (metadata_app_id, manifest_app_id)
            if value is not None and value != app_id
        }
    )
    if conflicting_ids:
        summary["identity_error"] = (
            f"workspace identity {app_id!r} conflicts with "
            + ", ".join(repr(value) for value in conflicting_ids)
        )
    return summary


def _read_mapping(path: Path, *, json_format: bool) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
        raw = json.loads(text) if json_format else yaml.safe_load(text)
    except (OSError, UnicodeError, json.JSONDecodeError, yaml.YAMLError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _read_readme_title(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("# "):
                return _nonempty_string(line[2:])
    except (OSError, UnicodeError):
        pass
    return None


def _nonempty_string(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _legacy_score(root: Path) -> tuple[int, int, float]:
    source_files = 0
    total_files = 0
    newest_mtime = 0.0
    try:
        stop = False
        for current, directories, filenames in os.walk(root, followlinks=False):
            directories[:] = sorted(
                name
                for name in directories
                if name not in _IGNORED_SCORE_PARTS
                and not (Path(current) / name).is_symlink()
            )
            for filename in sorted(filenames):
                path = Path(current) / filename
                if path.is_symlink() or not path.is_file():
                    continue
                total_files += 1
                newest_mtime = max(newest_mtime, path.stat().st_mtime)
                if path.name not in {"aios.deploy.yaml", APP_METADATA_NAME}:
                    source_files += 1
                if total_files >= 2_000:
                    stop = True
                    break
            if stop:
                break
    except OSError:
        pass
    return source_files, total_files, newest_mtime


def _workspace_has_source(root: Path) -> bool:
    if not root.is_dir():
        return False
    try:
        for current, directories, filenames in os.walk(root):
            directories[:] = [
                name for name in directories if name not in _IGNORED_SCORE_PARTS
            ]
            current_path = Path(current)
            for filename in filenames:
                path = current_path / filename
                relative = path.relative_to(root)
                if len(relative.parts) == 1 and filename in _ROOT_METADATA_FILES:
                    continue
                return True
        return False
    except OSError:
        return False


def _ensure_workspace_identity(root: Path, app_id: str) -> None:
    """Reject a durable root whose persisted identity points at another app."""

    if not root.is_dir():
        return
    declared = {
        "metadata": _nonempty_string(
            _read_mapping(root / APP_METADATA_NAME, json_format=True).get("app_id")
        ),
        "manifest": _nonempty_string(
            _read_mapping(root / "aios.deploy.yaml", json_format=False).get("app_id")
        ),
    }
    conflicts = [
        f"{source}={value}"
        for source, value in declared.items()
        if value is not None and value != app_id
    ]
    if conflicts:
        raise AppWorkspaceError(
            f"workspace {root} belongs to a different app ({', '.join(conflicts)})"
        )


def _legacy_chat_id(source: Path, session_dir: str | Path | None) -> str | None:
    sessions = (
        Path(session_dir).resolve()
        if session_dir is not None
        else get_sessions_dir().resolve()
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
        "# AIOS Projects\n\n"
        "Each directory is the durable source-of-truth project for one cloud app.\n"
        "Chat scratch contains conversation-specific files, not canonical project code.\n",
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
        "Keep application source and deployment configuration here. Chats may "
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
