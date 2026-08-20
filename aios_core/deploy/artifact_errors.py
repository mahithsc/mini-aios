"""Typed, agent-actionable failures for artifact creation."""

from __future__ import annotations


class ArtifactCreationError(RuntimeError):
    """Base failure surfaced by ``create_app_artifact``.

    ``code`` is stable for orchestration, while ``agent_instruction`` explains
    the recovery action in language the main agent can follow.
    """

    code = "artifact_creation_failed"
    retryable = False

    def __init__(self, message: str, *, agent_instruction: str) -> None:
        super().__init__(message)
        self.agent_instruction = agent_instruction


class InvalidArtifactHandoffError(ArtifactCreationError):
    code = "invalid_handoff"

    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            agent_instruction=(
                "Use only the exact handoff_id from a completed Codex "
                "workspace_handoff with status=handoff_ready. Do not invent or "
                "reconstruct a handoff ID."
            ),
        )


class ArtifactHandoffNotReadyError(ArtifactCreationError):
    code = "handoff_not_ready"
    retryable = True

    def __init__(self, current_status: object) -> None:
        super().__init__(
            f"Codex has not completed the artifact handoff (current status: "
            f"{current_status}).",
            agent_instruction=(
                "Wait for the Codex completion continuation. Retry only after its "
                "result has status=done and workspace_handoff.status=handoff_ready; "
                "then copy the exact completed workspace_handoff fields."
            ),
        )


class ArtifactHandoffIdentityMismatchError(ArtifactCreationError):
    code = "handoff_identity_mismatch"

    def __init__(self, mismatched_fields: list[str]) -> None:
        fields = ", ".join(mismatched_fields)
        super().__init__(
            f"Artifact arguments do not match the completed Codex handoff: {fields}.",
            agent_instruction=(
                "Call create_app_artifact again using the exact app_id, workspace_path, "
                "and full source_commit from the completed workspace_handoff. Never use "
                "latest, a branch, a tag, or an abbreviated commit."
            ),
        )


class ArtifactManifestRejectedError(ArtifactCreationError):
    code = "artifact_manifest_rejected"

    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            agent_instruction=(
                "Start a new Codex correction task for this app and have Codex fix and "
                "commit aios.deploy.yaml. Do not edit the app manifest directly."
            ),
        )


class DeploymentReceiptNotFoundError(ArtifactCreationError):
    """A downstream call did not receive an ID issued by its prerequisite."""

    retryable = False

    def __init__(self, receipt_type: str, receipt_id: str) -> None:
        self.code = f"{receipt_type}_not_ready"
        super().__init__(
            f"No ready {receipt_type} receipt exists for {receipt_id!r}.",
            agent_instruction=(
                f"Stop this deployment chain. Use only the exact {receipt_type}_id "
                f"returned by a successful prerequisite tool call; never invent one."
            ),
        )


class DeploymentReceiptMismatchError(ArtifactCreationError):
    code = "deployment_receipt_mismatch"

    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            agent_instruction=(
                "Stop this deployment chain and use IDs returned by the same artifact "
                "flow. Do not combine artifact, route, pipeline, or app IDs from "
                "different tool results."
            ),
        )
