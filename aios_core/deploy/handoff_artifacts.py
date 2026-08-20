"""Seal registered Codex workspaces into immutable cloud artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from .archive import create_artifact_archive
from .cloud_client import CloudDeployClient, DeploymentComponent
from .worktree_handoff import (
    WorktreeHandoffError,
    WorktreeRecord,
    WorktreeRegistry,
    WorktreeStatus,
)


class ArtifactHandoffReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = 1
    artifact_policy_version: int = 2
    artifact_id: str
    app_id: str
    source_commit: str
    source_tree: str
    provenance_commit: str | None = None
    worktree_id: str
    handoff_id: str
    inventory_sha256: str
    archive_sha256: str
    manifest_sha256: str
    archive_size: int
    file_count: int
    total_bytes: int
    verification_status: str
    components: list[DeploymentComponent]
    cleanup_status: WorktreeStatus


def create_uploaded_artifact_from_handoff(
    *,
    registry: WorktreeRegistry,
    cloud: CloudDeployClient,
    handoff_id: str,
) -> ArtifactHandoffReceipt:
    """Claim, validate, seal, upload, and clean one Codex worktree handoff."""

    artifact_run_id = uuid4().hex[:16]
    record: WorktreeRecord | None = None
    receipt_data: dict[str, Any] | None = None
    operation_error: Exception | None = None
    try:
        record = registry.claim_artifact(
            handoff_id,
            artifact_run_id=artifact_run_id,
        )
        app_id = record.app_id
        record = registry.transition(
            record.worktree_id,
            WorktreeStatus.VERIFYING,
            owner=record.owner,
            expected={WorktreeStatus.ARTIFACT_CLAIMED},
        )
        # The initial verification phase proves repository/commit/path identity.
        # Build-system-specific tests can be inserted here without changing the
        # handoff contract. Sanitization then guarantees tests cannot taint bytes.
        record = registry.sanitize_claimed(record)
        record = registry.transition(
            record.worktree_id,
            WorktreeStatus.SEALING,
            owner=record.owner,
            expected={WorktreeStatus.SANITIZING},
        )
        with tempfile.TemporaryDirectory(prefix="aios-handoff-artifact-") as directory:
            archive = create_artifact_archive(
                record.path,
                Path(directory) / "artifact.tar.gz",
            )
            # Detect source mutation across the archive pass before any bytes
            # leave the device. The archive digest remains the deployed-byte ID.
            registry.validate_claimed(record)
            if archive.manifest.app_id != app_id:
                raise WorktreeHandoffError(
                    "Deployment manifest app_id does not match workspace handoff"
                )
            manifest_json = json.dumps(
                archive.manifest.model_dump(mode="json", exclude_none=True),
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            artifact_id = cloud.upload_artifact(archive)
            if not re.fullmatch(r"art_[A-Za-z0-9]+", artifact_id):
                raise WorktreeHandoffError("Cloud returned an invalid artifact ID")
            components: list[DeploymentComponent] = [
                component
                for component in ("database", "server", "frontend")
                if getattr(archive.manifest, component) is not None
            ]
            receipt_data = {
                "artifact_id": artifact_id,
                "app_id": app_id,
                "source_commit": record.source_commit,
                "source_tree": record.source_tree,
                "provenance_commit": record.provenance_commit,
                "worktree_id": record.worktree_id,
                "handoff_id": record.handoff_id,
                "inventory_sha256": archive.source.sha256,
                "archive_sha256": archive.sha256,
                "manifest_sha256": hashlib.sha256(manifest_json).hexdigest(),
                "archive_size": archive.size,
                "file_count": archive.source.file_count,
                "total_bytes": archive.source.total_bytes,
                "verification_status": "source_identity_validated",
                "components": components,
            }
        record = registry.transition(
            record.worktree_id,
            WorktreeStatus.SEALED,
            owner=record.owner,
            expected={WorktreeStatus.SEALING},
        )
    except Exception as exc:  # noqa: BLE001 - failure is persisted before cleanup
        operation_error = exc
        if record is not None:
            try:
                record = registry.transition(
                    record.worktree_id,
                    WorktreeStatus.FAILED,
                    owner=record.owner,
                    expected={
                        WorktreeStatus.ARTIFACT_CLAIMED,
                        WorktreeStatus.VERIFYING,
                        WorktreeStatus.SANITIZING,
                        WorktreeStatus.SEALING,
                    },
                    error=str(exc),
                )
            except WorktreeHandoffError:
                pass
    finally:
        if record is not None:
            latest = registry.get(record.worktree_id)
            if latest.status in {
                WorktreeStatus.ARTIFACT_CLAIMED,
                WorktreeStatus.VERIFYING,
                WorktreeStatus.SANITIZING,
                WorktreeStatus.SEALING,
                WorktreeStatus.SEALED,
                WorktreeStatus.FAILED,
                WorktreeStatus.CLEANUP_FAILED,
            }:
                record = registry.cleanup(latest)

    if operation_error is not None:
        cleanup_detail = (
            f"; cleanup status: {record.status}" if record is not None else ""
        )
        raise WorktreeHandoffError(
            f"{operation_error}{cleanup_detail}"
        ) from operation_error
    if receipt_data is None or record is None:
        raise WorktreeHandoffError("Artifact handoff finished without a receipt")
    if record.status != WorktreeStatus.REMOVED:
        raise WorktreeHandoffError(
            f"Artifact was sealed but worktree cleanup ended in {record.status}"
        )
    receipt = ArtifactHandoffReceipt(
        **receipt_data,
        cleanup_status=record.status,
    )
    _persist_receipt(registry, receipt)
    return receipt


def _persist_receipt(
    registry: WorktreeRegistry, receipt: ArtifactHandoffReceipt
) -> None:
    directory = registry.root.parent / "artifacts" / "receipts"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / f"{receipt.artifact_id}.json"
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(receipt.model_dump_json(indent=2))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)
