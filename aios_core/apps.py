from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .workspace import resolve_workspace_path

APPS_DIR = "apps"
APPS_INDEX_PATH = "apps/apps.json"
DEFAULT_ENTRYPOINT_PATH = "src/instructions.md"
REGISTRY_VERSION = 1
APP_MANIFEST_VERSION = 1
APP_STATE_VERSION = 1
MANIFEST_STATUSES = {"active", "paused"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _apps_dir() -> Path:
    return resolve_workspace_path(APPS_DIR)


def _apps_index_path() -> Path:
    return resolve_workspace_path(APPS_INDEX_PATH)


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _slugify(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.strip().lower())
    normalized = normalized.strip("-")
    return normalized or "app"


def ensure_apps_workspace() -> Path:
    apps_dir = _apps_dir()
    apps_dir.mkdir(parents=True, exist_ok=True)
    index_path = _apps_index_path()
    if not index_path.exists():
        _write_json(index_path, {"version": REGISTRY_VERSION, "apps": []})
    return apps_dir


def load_apps_registry() -> dict[str, Any]:
    ensure_apps_workspace()
    payload = _load_json(_apps_index_path(), {"version": REGISTRY_VERSION, "apps": []})
    if not isinstance(payload, dict):
        return {"version": REGISTRY_VERSION, "apps": []}
    apps = payload.get("apps")
    if not isinstance(apps, list):
        apps = []
    return {
        "version": REGISTRY_VERSION,
        "apps": [entry for entry in apps if isinstance(entry, dict)],
    }


def save_apps_registry(registry: dict[str, Any]) -> None:
    payload = {
        "version": REGISTRY_VERSION,
        "apps": registry.get("apps", []),
    }
    _write_json(_apps_index_path(), payload)


def _registry_entries() -> list[dict[str, Any]]:
    return load_apps_registry()["apps"]


def _allocate_slug(name: str, requested_slug: str | None = None) -> str:
    base_slug = _slugify(requested_slug or name)
    used_slugs = {
        entry_slug
        for entry in _registry_entries()
        for entry_slug in [entry.get("slug")]
        if isinstance(entry_slug, str) and entry_slug
    }
    if base_slug not in used_slugs:
        return base_slug

    suffix = 2
    while f"{base_slug}-{suffix}" in used_slugs:
        suffix += 1
    return f"{base_slug}-{suffix}"


def _app_id_for_slug(slug: str) -> str:
    return f"app_{slug.replace('-', '_')}"


def _entry_paths(slug: str) -> tuple[Path, Path, Path]:
    app_dir = _apps_dir() / slug
    manifest_path = app_dir / "app_manifest.json"
    state_path = app_dir / "app_state.json"
    return app_dir, manifest_path, state_path


def _load_app_files(entry: dict[str, Any]) -> dict[str, Any]:
    manifest_path = resolve_workspace_path(str(entry["manifestPath"]))
    state_path = resolve_workspace_path(str(entry["statePath"]))
    manifest = _load_json(manifest_path, {})
    state = _load_json(state_path, {})
    return {
        "registry": entry,
        "manifest": manifest if isinstance(manifest, dict) else {},
        "state": state if isinstance(state, dict) else {},
    }


def list_apps() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for entry in _registry_entries():
        app = _load_app_files(entry)
        manifest = app["manifest"]
        state = app["state"]
        items.append(
            {
                "id": entry.get("id"),
                "slug": entry.get("slug"),
                "name": manifest.get("name"),
                "description": manifest.get("description"),
                "appType": manifest.get("appType", "static"),
                "status": manifest.get("status"),
                "port": manifest.get("port"),
                "startCommand": manifest.get("startCommand"),
                "runtimeStatus": state.get("runtime", {}).get("status")
                if isinstance(state.get("runtime"), dict)
                else None,
                "manifestPath": entry.get("manifestPath"),
                "statePath": entry.get("statePath"),
                "updatedAt": state.get("updatedAt") or entry.get("updatedAt"),
            }
        )
    return items


def get_app(app_id: str | None = None, slug: str | None = None) -> dict[str, Any] | None:
    if not app_id and not slug:
        raise ValueError("get requires app_id or slug")

    for entry in _registry_entries():
        if app_id and entry.get("id") == app_id:
            return _load_app_files(entry)
        if slug and entry.get("slug") == slug:
            return _load_app_files(entry)
    return None


APP_TYPES = {"static", "server"}


def create_app(
    *,
    name: str,
    description: str,
    entrypoint_content: str | None = None,
    slug: str | None = None,
    run_on_startup: bool = False,
    app_type: str = "static",
    port: int | None = None,
    start_command: str | None = None,
) -> dict[str, Any]:
    name = name.strip()
    description = description.strip()
    if not name:
        raise ValueError("create requires name")
    if not description:
        raise ValueError("create requires description")
    if app_type not in APP_TYPES:
        raise ValueError(f"app_type must be one of {sorted(APP_TYPES)}")

    ensure_apps_workspace()
    allocated_slug = _allocate_slug(name, requested_slug=slug)
    app_id = _app_id_for_slug(allocated_slug)
    created_at = _now_iso()

    app_dir, manifest_path, state_path = _entry_paths(allocated_slug)
    src_dir = app_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=False)

    instructions = (entrypoint_content or "").strip() or (
        f"# {name}\n\n"
        f"{description}\n\n"
        "Use this file as the primary app entrypoint instructions.\n"
    )
    (src_dir / "instructions.md").write_text(instructions, encoding="utf-8")

    manifest = {
        "version": APP_MANIFEST_VERSION,
        "id": app_id,
        "name": name,
        "description": description,
        "appType": app_type,
        "status": "paused",
        "entrypoint": DEFAULT_ENTRYPOINT_PATH,
        "runOnStartup": bool(run_on_startup),
        "port": port,
        "startCommand": start_command,
    }
    state = {
        "version": APP_STATE_VERSION,
        "appId": app_id,
        "summary": {
            "headline": "App created",
        },
        "domain": {},
        "runtime": {
            "status": "stopped",
            "initialized": False,
            "lastError": None,
        },
        "updatedAt": created_at,
    }

    _write_json(manifest_path, manifest)
    _write_json(state_path, state)

    registry = load_apps_registry()
    registry["apps"].append(
        {
            "id": app_id,
            "slug": allocated_slug,
            "manifestPath": f"apps/{allocated_slug}/app_manifest.json",
            "statePath": f"apps/{allocated_slug}/app_state.json",
            "createdAt": created_at,
            "updatedAt": created_at,
        }
    )
    save_apps_registry(registry)

    return {
        "id": app_id,
        "slug": allocated_slug,
        "path": f"apps/{allocated_slug}",
        "manifestPath": f"apps/{allocated_slug}/app_manifest.json",
        "statePath": f"apps/{allocated_slug}/app_state.json",
        "entrypoint": f"apps/{allocated_slug}/{DEFAULT_ENTRYPOINT_PATH}",
        "status": manifest["status"],
        "runOnStartup": manifest["runOnStartup"],
    }


def set_app_status(*, app_id: str | None = None, slug: str | None = None, status: str) -> dict[str, Any]:
    if status not in MANIFEST_STATUSES:
        raise ValueError(f"status must be one of {sorted(MANIFEST_STATUSES)}")

    app = get_app(app_id=app_id, slug=slug)
    if app is None:
        raise ValueError("app not found")

    manifest = app["manifest"]
    manifest["status"] = status
    _write_json(resolve_workspace_path(str(app["registry"]["manifestPath"])), manifest)

    registry = load_apps_registry()
    for entry in registry["apps"]:
        if entry.get("id") == app["registry"].get("id"):
            entry["updatedAt"] = _now_iso()
            break
    save_apps_registry(registry)

    return {
        "id": app["registry"].get("id"),
        "slug": app["registry"].get("slug"),
        "status": status,
    }


def read_app_state(*, app_id: str | None = None, slug: str | None = None) -> dict[str, Any]:
    app = get_app(app_id=app_id, slug=slug)
    if app is None:
        raise ValueError("app not found")
    return app["state"]
