from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class AppOrigin(StrEnum):
    BUILTIN = "builtin"
    USER = "user"
    AGENT = "agent"
    IMPORTED = "imported"


class AppStatus(StrEnum):
    DRAFT = "draft"
    NEEDS_PREPARE = "needs_prepare"
    READY = "ready"
    ENABLED = "enabled"
    UPDATE_PENDING = "update_pending"
    BROKEN = "broken"


@dataclass(frozen=True)
class SkillSpec:
    id: str
    path: str


@dataclass(frozen=True)
class McpServerSpec:
    id: str
    cwd: str
    command: tuple[str, ...]
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutableSpec:
    id: str
    cwd: str
    command: tuple[str, ...]
    timeout_seconds: int = 60


@dataclass(frozen=True)
class PrepareStep:
    command: tuple[str, ...]
    network: bool = False


@dataclass(frozen=True)
class RuntimeSpec:
    network: bool = False
    persistent_data: bool = False
    memory_mb: int = 512
    cpus: float = 1.0
    max_processes: int = 64


@dataclass(frozen=True)
class AppManifest:
    schema_version: int
    name: str
    description: str
    version: str
    skills: tuple[SkillSpec, ...] = ()
    mcp_servers: tuple[McpServerSpec, ...] = ()
    executables: tuple[ExecutableSpec, ...] = ()
    prepare: tuple[PrepareStep, ...] = ()
    runtime: RuntimeSpec = field(default_factory=RuntimeSpec)

    @property
    def requires_prepare(self) -> bool:
        return bool(self.prepare)

    @property
    def component_count(self) -> int:
        return len(self.skills) + len(self.mcp_servers) + len(self.executables)


@dataclass(frozen=True)
class Snapshot:
    content_hash: str
    path: Path
    file_count: int
    size_bytes: int


@dataclass(frozen=True)
class ValidatedApp:
    app: AppRecord
    manifest: AppManifest
    snapshot: Snapshot


@dataclass(frozen=True)
class AppRecord:
    id: str
    slug: str
    name: str
    description: str
    version: str
    origin: AppOrigin
    root_path: str
    manifest: AppManifest | None
    validated_hash: str | None
    prepared_hash: str | None
    active_hash: str | None
    enabled: bool
    network_approved: bool
    created_by_chat_id: str | None
    created_by_run_id: str | None
    created_at: int
    updated_at: int
    last_error: str | None

    @property
    def status(self) -> AppStatus:
        if self.validated_hash is None:
            return AppStatus.BROKEN if self.last_error else AppStatus.DRAFT
        if self.active_hash and self.active_hash != self.validated_hash:
            return AppStatus.UPDATE_PENDING
        if self.prepared_hash != self.validated_hash:
            return AppStatus.NEEDS_PREPARE
        if self.enabled and self.active_hash == self.validated_hash:
            return AppStatus.ENABLED
        return AppStatus.READY

    def to_dict(self) -> dict[str, Any]:
        components = {
            "skills": len(self.manifest.skills) if self.manifest else 0,
            "mcpServers": len(self.manifest.mcp_servers) if self.manifest else 0,
            "executables": len(self.manifest.executables) if self.manifest else 0,
        }
        return {
            "id": self.id,
            "slug": self.slug,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "origin": self.origin.value,
            "rootPath": self.root_path,
            "validatedHash": self.validated_hash,
            "preparedHash": self.prepared_hash,
            "activeHash": self.active_hash,
            "enabled": self.enabled,
            "networkApproved": self.network_approved,
            "status": self.status.value,
            "createdByChatId": self.created_by_chat_id,
            "createdByRunId": self.created_by_run_id,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "lastError": self.last_error,
            "components": components,
        }
