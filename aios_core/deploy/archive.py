"""Deterministic cloud artifact archive construction."""

from __future__ import annotations

import gzip
import hashlib
import os
import secrets
import tarfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from .manifest import (
    ArtifactInventory,
    DeploymentManifest,
    ManifestValidationError,
    artifact_file_paths,
    validate_cloud_artifact,
)


class ArtifactArchive(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: Path
    sha256: str
    size: int
    source: ArtifactInventory
    manifest: DeploymentManifest


def create_artifact_archive(
    app_dir: str | Path,
    destination: str | Path,
) -> ArtifactArchive:
    root = Path(app_dir).resolve()
    output = Path(destination).resolve()
    try:
        output.relative_to(root)
    except ValueError:
        pass
    else:
        raise ManifestValidationError(
            "Artifact archive must be created outside the app"
        )

    manifest, inventory = validate_cloud_artifact(root)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{secrets.token_hex(6)}.tmp")
    try:
        with (
            temporary.open("wb") as raw,
            gzip.GzipFile(
                filename="",
                mode="wb",
                fileobj=raw,
                mtime=0,
            ) as compressed,
            tarfile.open(
                fileobj=compressed,
                mode="w|",
                format=tarfile.PAX_FORMAT,
            ) as archive,
        ):
            for path in artifact_file_paths(root):
                relative = path.relative_to(root).as_posix()
                info = archive.gettarinfo(str(path), arcname=relative)
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                info.mtime = 0
                info.mode = 0o755 if os.access(path, os.X_OK) else 0o644
                info.pax_headers = {}
                with path.open("rb") as source:
                    archive.addfile(info, source)
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)

    digest = hashlib.sha256()
    with output.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return ArtifactArchive(
        path=output,
        sha256=digest.hexdigest(),
        size=output.stat().st_size,
        source=inventory,
        manifest=manifest,
    )
