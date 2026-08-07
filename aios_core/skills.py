from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .skill_limits import (
    MAX_SKILL_FILE_BYTES,
    MAX_SKILL_NAME_CHARS,
    MAX_SKILL_SUMMARY_CHARS,
    MAX_SKILL_TITLE_CHARS,
)
from .workspace import get_skills_dir

_SKILL_FILE_NAME = "SKILL.md"
_IGNORED_DIR_PREFIXES = (".", "_")
_IGNORED_ROOT_FILES = {"README.md", "skills_index.json"}


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n")


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    normalized = _normalize_newlines(text)
    if not normalized.startswith("---\n"):
        return {}, normalized

    _, _, remainder = normalized.partition("---\n")
    frontmatter_block, separator, body = remainder.partition("\n---\n")
    if not separator:
        return {}, normalized

    metadata: dict[str, str] = {}
    for raw_line in frontmatter_block.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition(":")
        if not separator:
            continue
        metadata[key.strip().lower()] = value.strip()
    return metadata, body.lstrip("\n")


def _extract_title(body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return ""


def _is_ignored_path(path: Path, skills_dir: Path) -> bool:
    try:
        relative = path.relative_to(skills_dir)
    except ValueError:
        return True

    for part in relative.parts[:-1]:
        if part.startswith(_IGNORED_DIR_PREFIXES):
            return True
    return False


def _agent_skill_path(path: Path, skills_dir: Path) -> str:
    return f"skills/{path.relative_to(skills_dir).as_posix()}"


def _skill_name_from_path(path: Path, skills_dir: Path) -> str:
    if path.name == _SKILL_FILE_NAME and path.parent != skills_dir:
        return path.parent.name
    return path.stem


def _load_skill_from_file(
    path: Path,
    skills_dir: Path,
    overrides: dict[str, Any] | None = None,
    *,
    agent_file: str | None = None,
) -> dict[str, str] | None:
    if not path.exists() or not path.is_file():
        return None
    if _is_ignored_path(path, skills_dir):
        return None

    try:
        if path.stat().st_size > MAX_SKILL_FILE_BYTES:
            return None
        with path.open(encoding="utf-8") as file:
            text = file.read(MAX_SKILL_FILE_BYTES + 1)
    except (OSError, UnicodeError):
        return None
    if len(text.encode("utf-8")) > MAX_SKILL_FILE_BYTES:
        return None
    metadata, body = _parse_frontmatter(text)
    overrides = overrides or {}

    name = str(
        overrides.get("name")
        or metadata.get("name")
        or _skill_name_from_path(path, skills_dir)
    ).strip()
    if not name or len(name) > MAX_SKILL_NAME_CHARS:
        return None

    title = str(
        overrides.get("title") or metadata.get("title") or _extract_title(body) or name
    ).strip()
    description = str(
        overrides.get("summary")
        or overrides.get("description")
        or metadata.get("description")
        or ""
    ).strip()
    title = title[:MAX_SKILL_TITLE_CHARS]
    description = description[:MAX_SKILL_SUMMARY_CHARS]

    return {
        "name": name,
        "title": title or name,
        "summary": description,
        "file": agent_file or _agent_skill_path(path, skills_dir),
    }


def _normalize_manifest_path(raw_path: str, skills_dir: Path) -> Path:
    candidate = Path(raw_path).expanduser()
    if candidate.is_absolute():
        return candidate

    relative_path = raw_path.strip().replace("\\", "/")
    relative_path = relative_path.removeprefix("skills/")
    return skills_dir / relative_path


def _manifest_name_candidates(name: str, skills_dir: Path) -> list[Path]:
    return [
        skills_dir / name / _SKILL_FILE_NAME,
        skills_dir / f"{name}.md",
    ]


def _manifest_entry_to_skill(
    entry: str | dict[str, Any], skills_dir: Path
) -> dict[str, str] | None:
    if isinstance(entry, str):
        name = entry.strip()
        if not name:
            return None
        for candidate in _manifest_name_candidates(name, skills_dir):
            skill = _load_skill_from_file(candidate, skills_dir, {"name": name})
            if skill:
                return skill
        return None

    if not isinstance(entry, dict):
        return None
    if entry.get("enabled") is False:
        return None

    raw_path = str(entry.get("file") or entry.get("path") or "").strip()
    if raw_path:
        candidate_paths = [_normalize_manifest_path(raw_path, skills_dir)]
    else:
        name = str(
            entry.get("name") or entry.get("id") or entry.get("title") or ""
        ).strip()
        candidate_paths = _manifest_name_candidates(name, skills_dir) if name else []

    for candidate in candidate_paths:
        skill = _load_skill_from_file(candidate, skills_dir, entry)
        if skill:
            return skill
    return None


def _discover_skill_files(skills_dir: Path) -> list[Path]:
    discovered = []

    for path in sorted(skills_dir.glob("*/SKILL.md")):
        if not _is_ignored_path(path, skills_dir):
            discovered.append(path)

    for path in sorted(skills_dir.glob("*.md")):
        if path.name in _IGNORED_ROOT_FILES:
            continue
        if not _is_ignored_path(path, skills_dir):
            discovered.append(path)

    return discovered


def _load_manifest_entries(skills_dir: Path) -> list[str | dict[str, Any]]:
    try:
        with open(skills_dir / "skills_index.json", encoding="utf-8") as file:
            raw_manifest = json.load(file)
    except (FileNotFoundError, json.JSONDecodeError):
        return []

    if isinstance(raw_manifest, dict):
        raw_manifest = raw_manifest.get("skills", [])
    if not isinstance(raw_manifest, list):
        return []
    return raw_manifest


def _load_global_skills() -> list[dict[str, str]]:
    skills_dir = get_skills_dir()
    skills: list[dict[str, str]] = []
    seen_files: set[str] = set()

    for entry in _load_manifest_entries(skills_dir):
        skill = _manifest_entry_to_skill(entry, skills_dir)
        if not skill:
            continue
        file_path = skill["file"]
        if file_path in seen_files:
            continue
        seen_files.add(file_path)
        skills.append(skill)

    for path in _discover_skill_files(skills_dir):
        skill = _load_skill_from_file(path, skills_dir)
        if not skill:
            continue
        file_path = skill["file"]
        if file_path in seen_files:
            continue
        seen_files.add(file_path)
        skills.append(skill)

    return skills


def _load_app_skills() -> list[dict[str, str]]:
    """Load enabled App skills strictly from immutable active snapshots."""

    try:
        from .apps.manifest import load_manifest
        from .apps.service import AppService

        service = AppService()
        apps = service.registry.list(enabled=True)
    except Exception as exc:  # noqa: BLE001 - Apps remain optional at startup
        print(f"[apps] skills could not be loaded: {exc}")
        return []

    skills: list[dict[str, str]] = []
    for app in apps:
        if not app.active_hash:
            continue
        try:
            snapshot_root = service.verify_snapshot(app, app.active_hash).path
            manifest = load_manifest(snapshot_root / "app.json")
        except Exception as exc:  # noqa: BLE001 - isolate one broken App
            print(f"[apps] skills for {app.slug} could not be loaded: {exc}")
            continue
        for spec in manifest.skills:
            namespace = f"{app.slug}/{spec.id}"
            skill = _load_skill_from_file(
                snapshot_root / spec.path,
                snapshot_root,
                {"name": namespace},
                agent_file=f"app://{namespace}",
            )
            if skill is None:
                continue
            skill["title"] = f"{app.name}: {skill['title']}"
            skills.append(skill)
    return skills


def resolve_skill_file(file_path: str) -> Path | None:
    if not file_path.startswith("app://"):
        skills_dir = get_skills_dir().resolve()
        relative_path = file_path.replace("\\", "/")
        relative_path = relative_path.removeprefix("skills/")
        candidate = (skills_dir / relative_path).resolve()
        try:
            candidate.relative_to(skills_dir)
        except ValueError:
            return None
        return candidate

    namespace = file_path[len("app://") :]
    slug, separator, skill_id = namespace.partition("/")
    if not separator or not slug or not skill_id or "/" in skill_id:
        return None
    try:
        from .apps.manifest import load_manifest
        from .apps.service import AppService

        service = AppService()
        app = service.registry.require(slug)
        if not app.enabled or not app.active_hash:
            return None
        snapshot_root = service.verify_snapshot(app, app.active_hash).path.resolve()
        manifest = load_manifest(snapshot_root / "app.json")
        spec = next((skill for skill in manifest.skills if skill.id == skill_id), None)
        if spec is None:
            return None
        candidate = (snapshot_root / spec.path).resolve()
        candidate.relative_to(snapshot_root)
        return candidate
    except Exception:  # noqa: BLE001 - unavailable/disabled App skill
        return None


def load_skills() -> list[dict[str, str]]:
    return [*_load_global_skills(), *_load_app_skills()]
