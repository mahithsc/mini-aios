"""Cloud deployment artifact manifest and local safety validation."""

from __future__ import annotations

import hashlib
import re
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Annotated

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

MANIFEST_NAME = "aios.deploy.yaml"

_ENV_NAME = r"^[A-Za-z_][A-Za-z0-9_]*$"
_REFERENCE = r"^[A-Za-z][A-Za-z0-9_.:/-]*$"
_EXTENSION = r"^[a-z][a-z0-9_]*$"
_IGNORED_PARTS = {
    ".git",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "node_modules",
}
_SAFE_ENV_TEMPLATES = {".env.example", ".env.sample", ".env.template"}
_FORBIDDEN_SUFFIXES = {".key", ".p12", ".pem", ".pfx"}
_HIGH_CONFIDENCE_SECRET_PATTERNS = (
    re.compile(rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(rb"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(rb"\bsk_live_[A-Za-z0-9]{16,}\b"),
)


class ManifestValidationError(ValueError):
    """The local app artifact does not satisfy the cloud deploy contract."""


class SecretExposure(StrEnum):
    RUNTIME = "runtime"
    BUILD = "build"


class SecretBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    env: str = Field(pattern=_ENV_NAME)
    secret_ref: str = Field(pattern=r"^sec_[A-Za-z0-9]+$")
    exposure: SecretExposure = SecretExposure.RUNTIME


class PublicConfigBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    env: str = Field(pattern=_ENV_NAME)
    config_ref: str = Field(pattern=_REFERENCE)


class DatabaseComponent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    migrations: str = "database/migrations"
    required_extensions: list[str] = Field(default_factory=list)

    @field_validator("migrations")
    @classmethod
    def validate_migrations_path(cls, value: str) -> str:
        return _relative_path(value)

    @field_validator("required_extensions")
    @classmethod
    def validate_extensions(cls, values: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(values))
        if any(not re.fullmatch(_EXTENSION, value) for value in normalized):
            raise ValueError("Extension names must be lowercase Postgres identifiers")
        return normalized


class ServerComponent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = "server"
    dockerfile: str = "server/Dockerfile"
    health_path: str = "/health"
    secrets: list[SecretBinding] = Field(default_factory=list)

    @field_validator("source", "dockerfile")
    @classmethod
    def validate_paths(cls, value: str) -> str:
        return _relative_path(value)

    @field_validator("health_path")
    @classmethod
    def validate_health_path(cls, value: str) -> str:
        if (
            not value.startswith("/")
            or "?" in value
            or "#" in value
            or len(value) > 2048
            or any(character.isspace() for character in value)
        ):
            raise ValueError("health_path must be an absolute URL path")
        return value

    @model_validator(mode="after")
    def require_runtime_secrets(self) -> ServerComponent:
        if any(binding.exposure == SecretExposure.BUILD for binding in self.secrets):
            raise ValueError("Server secrets must use runtime exposure")
        if any(
            binding.env in {"PORT", "AIOS_DEPLOYMENT_ID"}
            for binding in self.secrets
        ):
            raise ValueError("Server secret uses a reserved environment name")
        return self


class FrontendComponent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = "frontend"
    secrets: list[SecretBinding] = Field(default_factory=list)
    public_config: list[PublicConfigBinding] = Field(default_factory=list)

    @field_validator("source")
    @classmethod
    def validate_source(cls, value: str) -> str:
        return _relative_path(value)


class DeploymentManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Annotated[int, Field(strict=True)]
    app_id: str = Field(pattern=r"^app_[A-Za-z0-9]+$")
    database: DatabaseComponent | None = None
    server: ServerComponent | None = None
    frontend: FrontendComponent | None = None

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: int) -> int:
        if value != 1:
            raise ValueError("Only deployment manifest version 1 is supported")
        return value

    @model_validator(mode="after")
    def require_component(self) -> DeploymentManifest:
        if self.database is None and self.server is None and self.frontend is None:
            raise ValueError("At least one deployable component is required")
        return self


class ArtifactInventory(BaseModel):
    model_config = ConfigDict(frozen=True)

    sha256: str
    file_count: int
    total_bytes: int


def load_deployment_manifest(app_dir: str | Path) -> DeploymentManifest:
    root = Path(app_dir).resolve()
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.is_file():
        raise ManifestValidationError(f"Missing {MANIFEST_NAME}")
    try:
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ManifestValidationError(f"Could not read {MANIFEST_NAME}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ManifestValidationError(f"{MANIFEST_NAME} must contain a YAML object")
    try:
        manifest = DeploymentManifest.model_validate(raw)
    except ValidationError as exc:
        raise ManifestValidationError(str(exc)) from exc
    _validate_component_paths(root, manifest)
    return manifest


def validate_artifact_tree(
    app_dir: str | Path,
    *,
    max_files: int = 10_000,
    max_total_bytes: int = 1024 * 1024 * 1024,
) -> ArtifactInventory:
    root = Path(app_dir).resolve()
    if not root.is_dir():
        raise ManifestValidationError("Artifact root is not a directory")

    digest = hashlib.sha256()
    file_count = 0
    total_bytes = 0
    for path in artifact_file_paths(root):
        relative = path.relative_to(root)
        file_count += 1
        if file_count > max_files:
            raise ManifestValidationError(f"Artifact exceeds {max_files} files")
        size = path.stat().st_size
        total_bytes += size
        if total_bytes > max_total_bytes:
            raise ManifestValidationError(
                f"Artifact exceeds {max_total_bytes} total bytes"
            )

        relative_bytes = relative.as_posix().encode("utf-8")
        digest.update(len(relative_bytes).to_bytes(4, "big"))
        digest.update(relative_bytes)
        digest.update(size.to_bytes(8, "big"))
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                if any(
                    pattern.search(chunk)
                    for pattern in _HIGH_CONFIDENCE_SECRET_PATTERNS
                ):
                    raise ManifestValidationError(
                        f"Possible credential material found in {relative}"
                    )
                digest.update(chunk)

    if file_count == 0:
        raise ManifestValidationError("Artifact contains no files")
    return ArtifactInventory(
        sha256=digest.hexdigest(),
        file_count=file_count,
        total_bytes=total_bytes,
    )


def artifact_file_paths(app_dir: str | Path) -> list[Path]:
    root = Path(app_dir).resolve()
    if not root.is_dir():
        raise ManifestValidationError("Artifact root is not a directory")
    files: list[Path] = []
    for path in sorted(root.rglob("*"), key=lambda candidate: candidate.as_posix()):
        relative = path.relative_to(root)
        if any(part in _IGNORED_PARTS for part in relative.parts):
            continue
        if path.is_symlink():
            raise ManifestValidationError(f"Symbolic links are not allowed: {relative}")
        if not path.is_file():
            continue
        _validate_artifact_filename(relative)
        files.append(path)
    return files


def validate_cloud_artifact(
    app_dir: str | Path,
) -> tuple[DeploymentManifest, ArtifactInventory]:
    manifest = load_deployment_manifest(app_dir)
    inventory = validate_artifact_tree(app_dir)
    return manifest, inventory


def _relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or value != path.as_posix()
    ):
        raise ValueError("Path must be a normalized relative POSIX path")
    return value


def _validate_component_paths(root: Path, manifest: DeploymentManifest) -> None:
    expected: list[tuple[str, str, bool]] = []
    if manifest.database is not None:
        expected.append(("database.migrations", manifest.database.migrations, True))
    if manifest.server is not None:
        expected.extend(
            (
                ("server.source", manifest.server.source, True),
                ("server.dockerfile", manifest.server.dockerfile, False),
            )
        )
    if manifest.frontend is not None:
        expected.append(("frontend.source", manifest.frontend.source, True))

    for label, relative, directory in expected:
        candidate = root / relative
        valid = candidate.is_dir() if directory else candidate.is_file()
        if not valid:
            expected_type = "directory" if directory else "file"
            raise ManifestValidationError(
                f"{label} does not reference an existing {expected_type}: {relative}"
            )


def _validate_artifact_filename(relative: Path) -> None:
    name = relative.name
    if name == ".env" or (name.startswith(".env.") and name not in _SAFE_ENV_TEMPLATES):
        raise ManifestValidationError(f"Environment file is not allowed: {relative}")
    if relative.suffix.lower() in _FORBIDDEN_SUFFIXES:
        raise ManifestValidationError(f"Credential file is not allowed: {relative}")
