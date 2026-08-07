from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from .models import (
    AppManifest,
    ExecutableSpec,
    McpServerSpec,
    PrepareStep,
    RuntimeSpec,
    SkillSpec,
)

MANIFEST_FILENAME = "app.json"
SUPPORTED_SCHEMA_VERSION = 1
MAX_MANIFEST_BYTES = 1024 * 1024

_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_RESERVED_ENV_NAMES = {
    "HOME",
    "HOSTNAME",
    "NODE_PATH",
    "PATH",
    "PWD",
    "PYTHONHOME",
    "PYTHONPATH",
    "TMPDIR",
}


class ManifestValidationError(ValueError):
    """Raised when an App manifest is malformed or unsafe."""


def validate_slug(value: str) -> str:
    if not isinstance(value, str) or not _ID_PATTERN.fullmatch(value):
        raise ManifestValidationError(
            "slug must start with a lowercase letter and contain only "
            "lowercase letters, digits, hyphens, or underscores"
        )
    return value


def _object(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ManifestValidationError(f"{field} must be an object")
    return value


def _array(value: object, field: str) -> list[object]:
    if not isinstance(value, list):
        raise ManifestValidationError(f"{field} must be an array")
    return value


def _reject_unknown(
    value: Mapping[str, Any],
    allowed: set[str],
    field: str,
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ManifestValidationError(
            f"{field} contains unknown field(s): {', '.join(unknown)}"
        )


def _string(
    value: object,
    field: str,
    *,
    allow_empty: bool = False,
    max_length: int = 2048,
) -> str:
    if not isinstance(value, str):
        raise ManifestValidationError(f"{field} must be a string")
    normalized = value.strip()
    if not allow_empty and not normalized:
        raise ManifestValidationError(f"{field} cannot be empty")
    if len(normalized) > max_length:
        raise ManifestValidationError(f"{field} cannot exceed {max_length} characters")
    return normalized


def _boolean(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise ManifestValidationError(f"{field} must be a boolean")
    return value


def _integer(
    value: object,
    field: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ManifestValidationError(f"{field} must be an integer")
    if not minimum <= value <= maximum:
        raise ManifestValidationError(
            f"{field} must be between {minimum} and {maximum}"
        )
    return value


def _number(
    value: object,
    field: str,
    *,
    minimum: float,
    maximum: float,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ManifestValidationError(f"{field} must be a number")
    result = float(value)
    if not minimum <= result <= maximum:
        raise ManifestValidationError(
            f"{field} must be between {minimum:g} and {maximum:g}"
        )
    return result


def _component_id(value: object, field: str) -> str:
    result = _string(value, field, max_length=64)
    if not _ID_PATTERN.fullmatch(result):
        raise ManifestValidationError(
            f"{field} must start with a lowercase letter and contain only "
            "lowercase letters, digits, hyphens, or underscores"
        )
    return result


def _relative_path(
    value: object,
    field: str,
    *,
    allow_root: bool = False,
) -> str:
    result = _string(value, field, max_length=512)
    if "\\" in result:
        raise ManifestValidationError(f"{field} must use forward slashes")
    path = PurePosixPath(result)
    if path.is_absolute() or any(part == ".." for part in path.parts):
        raise ManifestValidationError(
            f"{field} must be relative and cannot contain '..'"
        )
    if any(part in ("", ".") for part in path.parts) and not (
        allow_root and result == "."
    ):
        raise ManifestValidationError(f"{field} contains an invalid path segment")
    return path.as_posix()


def _command(value: object, field: str) -> tuple[str, ...]:
    items = _array(value, field)
    if not items:
        raise ManifestValidationError(f"{field} cannot be empty")
    if len(items) > 128:
        raise ManifestValidationError(f"{field} cannot contain more than 128 items")
    command: list[str] = []
    for index, item in enumerate(items):
        command.append(_string(item, f"{field}[{index}]", max_length=4096))
    return tuple(command)


def _environment(value: object, field: str) -> dict[str, str]:
    mapping = _object(value, field)
    if len(mapping) > 64:
        raise ManifestValidationError(f"{field} cannot contain more than 64 entries")
    result: dict[str, str] = {}
    for raw_name, raw_value in mapping.items():
        name = _string(raw_name, f"{field} key", max_length=128)
        if name in _RESERVED_ENV_NAMES or name.startswith("AIOS_"):
            raise ManifestValidationError(f"{field}.{name} is reserved by AIOS")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ManifestValidationError(
                f"{field}.{name} is not a valid environment name"
            )
        result[name] = _string(
            raw_value,
            f"{field}.{name}",
            allow_empty=True,
            max_length=8192,
        )
    return result


def _register_id(component_id: str, field: str, seen: set[str]) -> None:
    if component_id in seen:
        raise ManifestValidationError(
            f"duplicate component id '{component_id}' in {field}"
        )
    seen.add(component_id)


def parse_manifest(data: Mapping[str, Any]) -> AppManifest:
    root = _object(data, "manifest")
    _reject_unknown(
        root,
        {
            "schemaVersion",
            "name",
            "description",
            "version",
            "skills",
            "mcpServers",
            "executables",
            "prepare",
            "runtime",
        },
        "manifest",
    )
    schema_version = _integer(
        root.get("schemaVersion"),
        "schemaVersion",
        minimum=SUPPORTED_SCHEMA_VERSION,
        maximum=SUPPORTED_SCHEMA_VERSION,
    )
    name = _string(root.get("name"), "name", max_length=100)
    description = _string(
        root.get("description", ""),
        "description",
        allow_empty=True,
        max_length=2000,
    )
    version = _string(root.get("version"), "version", max_length=64)

    seen_ids: set[str] = set()
    skills: list[SkillSpec] = []
    for index, raw_skill in enumerate(_array(root.get("skills", []), "skills")):
        field = f"skills[{index}]"
        skill = _object(raw_skill, field)
        _reject_unknown(skill, {"id", "path"}, field)
        component_id = _component_id(skill.get("id"), f"{field}.id")
        _register_id(component_id, field, seen_ids)
        skills.append(
            SkillSpec(
                id=component_id,
                path=_relative_path(skill.get("path"), f"{field}.path"),
            )
        )

    mcp_servers: list[McpServerSpec] = []
    for index, raw_server in enumerate(
        _array(root.get("mcpServers", []), "mcpServers")
    ):
        field = f"mcpServers[{index}]"
        server = _object(raw_server, field)
        _reject_unknown(server, {"id", "cwd", "command", "env"}, field)
        component_id = _component_id(server.get("id"), f"{field}.id")
        _register_id(component_id, field, seen_ids)
        mcp_servers.append(
            McpServerSpec(
                id=component_id,
                cwd=_relative_path(
                    server.get("cwd", "."),
                    f"{field}.cwd",
                    allow_root=True,
                ),
                command=_command(server.get("command"), f"{field}.command"),
                env=_environment(server.get("env", {}), f"{field}.env"),
            )
        )

    executables: list[ExecutableSpec] = []
    for index, raw_executable in enumerate(
        _array(root.get("executables", []), "executables")
    ):
        field = f"executables[{index}]"
        executable = _object(raw_executable, field)
        _reject_unknown(
            executable,
            {"id", "cwd", "command", "timeoutSeconds"},
            field,
        )
        component_id = _component_id(executable.get("id"), f"{field}.id")
        _register_id(component_id, field, seen_ids)
        executables.append(
            ExecutableSpec(
                id=component_id,
                cwd=_relative_path(
                    executable.get("cwd", "."),
                    f"{field}.cwd",
                    allow_root=True,
                ),
                command=_command(
                    executable.get("command"),
                    f"{field}.command",
                ),
                timeout_seconds=_integer(
                    executable.get("timeoutSeconds", 60),
                    f"{field}.timeoutSeconds",
                    minimum=1,
                    maximum=3600,
                ),
            )
        )

    prepare: list[PrepareStep] = []
    for index, raw_step in enumerate(_array(root.get("prepare", []), "prepare")):
        field = f"prepare[{index}]"
        step = _object(raw_step, field)
        _reject_unknown(step, {"command", "network"}, field)
        prepare.append(
            PrepareStep(
                command=_command(step.get("command"), f"{field}.command"),
                network=_boolean(step.get("network", False), f"{field}.network"),
            )
        )
    if len(prepare) > 32:
        raise ManifestValidationError("prepare cannot contain more than 32 steps")

    raw_runtime = _object(root.get("runtime", {}), "runtime")
    _reject_unknown(
        raw_runtime,
        {"network", "persistentData", "memoryMb", "cpus", "maxProcesses"},
        "runtime",
    )
    runtime = RuntimeSpec(
        network=_boolean(raw_runtime.get("network", False), "runtime.network"),
        persistent_data=_boolean(
            raw_runtime.get("persistentData", False),
            "runtime.persistentData",
        ),
        memory_mb=_integer(
            raw_runtime.get("memoryMb", 512),
            "runtime.memoryMb",
            minimum=64,
            maximum=16384,
        ),
        cpus=_number(
            raw_runtime.get("cpus", 1.0),
            "runtime.cpus",
            minimum=0.1,
            maximum=16.0,
        ),
        max_processes=_integer(
            raw_runtime.get("maxProcesses", 64),
            "runtime.maxProcesses",
            minimum=1,
            maximum=1024,
        ),
    )
    if sum((len(skills), len(mcp_servers), len(executables))) > 256:
        raise ManifestValidationError(
            "manifest cannot declare more than 256 components"
        )

    return AppManifest(
        schema_version=schema_version,
        name=name,
        description=description,
        version=version,
        skills=tuple(skills),
        mcp_servers=tuple(mcp_servers),
        executables=tuple(executables),
        prepare=tuple(prepare),
        runtime=runtime,
    )


def load_manifest(path: Path) -> AppManifest:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ManifestValidationError(
            f"could not read {MANIFEST_FILENAME}: {exc}"
        ) from exc
    if size > MAX_MANIFEST_BYTES:
        raise ManifestValidationError(
            f"{MANIFEST_FILENAME} cannot exceed {MAX_MANIFEST_BYTES} bytes"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ManifestValidationError(f"invalid {MANIFEST_FILENAME}: {exc}") from exc
    return parse_manifest(_object(data, "manifest"))


def manifest_to_dict(manifest: AppManifest) -> dict[str, Any]:
    return {
        "schemaVersion": manifest.schema_version,
        "name": manifest.name,
        "description": manifest.description,
        "version": manifest.version,
        "skills": [{"id": skill.id, "path": skill.path} for skill in manifest.skills],
        "mcpServers": [
            {
                "id": server.id,
                "cwd": server.cwd,
                "command": list(server.command),
                "env": dict(server.env),
            }
            for server in manifest.mcp_servers
        ],
        "executables": [
            {
                "id": executable.id,
                "cwd": executable.cwd,
                "command": list(executable.command),
                "timeoutSeconds": executable.timeout_seconds,
            }
            for executable in manifest.executables
        ],
        "prepare": [
            {"command": list(step.command), "network": step.network}
            for step in manifest.prepare
        ],
        "runtime": {
            "network": manifest.runtime.network,
            "persistentData": manifest.runtime.persistent_data,
            "memoryMb": manifest.runtime.memory_mb,
            "cpus": manifest.runtime.cpus,
            "maxProcesses": manifest.runtime.max_processes,
        },
    }


def canonical_manifest_json(manifest: AppManifest) -> str:
    return json.dumps(
        manifest_to_dict(manifest),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def referenced_paths(manifest: AppManifest) -> Iterable[tuple[str, str]]:
    for skill in manifest.skills:
        yield skill.path, "file"
    for server in manifest.mcp_servers:
        yield server.cwd, "directory"
    for executable in manifest.executables:
        yield executable.cwd, "directory"
