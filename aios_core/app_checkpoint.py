"""Legacy v2 schema validation for in-repository AIOS checkpoints.

New contract-v3 jobs persist host-owned B/C records in ``CodexRunStore`` and do
not create metadata commit M. This parser remains for existing v2 histories and
in-flight job recovery; those repositories are never rewritten during migration.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .app_git import AppGitError, list_changed_files, resolve_commit

FullGitOid = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
NonEmptyString = Annotated[str, Field(min_length=1)]


class AppCheckpointError(ValueError):
    """A checkpoint does not match the canonical schema or expected Git state."""


class AppCheckpoint(BaseModel):
    """The accepted legacy checkpoint representation for ``B -> C -> M``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    job_id: NonEmptyString
    app_id: NonEmptyString
    base_commit: FullGitOid
    change_commit: FullGitOid
    summary: NonEmptyString
    changed_files: Annotated[list[NonEmptyString], Field(min_length=1)]
    verification: Annotated[list[NonEmptyString], Field(min_length=1)]

    @field_validator("changed_files")
    @classmethod
    def validate_changed_files(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("changed_files must not contain duplicates")
        for value in values:
            path = PurePosixPath(value)
            if path.is_absolute() or ".." in path.parts or "\\" in value:
                raise ValueError("changed_files entries must be relative POSIX paths")
        return values


def _validation_message(error: ValidationError) -> str:
    problems = []
    for item in error.errors(include_url=False):
        location = ".".join(str(part) for part in item["loc"]) or "checkpoint"
        problems.append(f"{location}: {item['msg']}")
    return "; ".join(problems)


def load_app_checkpoint(path: str | Path) -> AppCheckpoint:
    """Load a checkpoint using the same strict model used by host validation."""

    checkpoint_path = Path(path)
    try:
        payload = checkpoint_path.read_text(encoding="utf-8")
        return AppCheckpoint.model_validate_json(payload)
    except FileNotFoundError as exc:
        raise AppCheckpointError(f"Missing checkpoint: {checkpoint_path}") from exc
    except (OSError, UnicodeError) as exc:
        raise AppCheckpointError(f"Could not read checkpoint: {exc}") from exc
    except ValidationError as exc:
        raise AppCheckpointError(
            f"Checkpoint schema is invalid: {_validation_message(exc)}"
        ) from exc


def validate_app_checkpoint(
    checkpoint: AppCheckpoint,
    *,
    repository: str | Path,
    job_id: str,
    app_id: str,
    base_commit: str,
    change_commit: str,
) -> AppCheckpoint:
    """Validate checkpoint identity and its exact substantive changed-file set."""

    root = Path(repository).resolve()
    try:
        expected_base = resolve_commit(root, base_commit)
        expected_change = resolve_commit(root, change_commit)
        actual_files = list_changed_files(root, expected_base, expected_change)
    except AppGitError as exc:
        raise AppCheckpointError(
            f"Could not validate checkpoint Git state: {exc}"
        ) from exc
    if checkpoint.job_id != job_id:
        raise AppCheckpointError("Checkpoint job_id does not match this Codex job")
    if checkpoint.app_id != app_id:
        raise AppCheckpointError("Checkpoint app_id does not match the app")
    if checkpoint.base_commit != expected_base:
        raise AppCheckpointError("Checkpoint base_commit does not match B")
    if checkpoint.change_commit != expected_change:
        raise AppCheckpointError("Checkpoint change_commit does not match C")
    if sorted(checkpoint.changed_files) != sorted(actual_files):
        raise AppCheckpointError(
            "Checkpoint changed_files does not exactly match the B-to-C diff"
        )
    return checkpoint


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate an AIOS app checkpoint before committing metadata M."
    )
    parser.add_argument("command", choices=["validate"])
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--app-id", required=True)
    parser.add_argument("--base-commit", required=True)
    parser.add_argument("--change-commit", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        checkpoint = load_app_checkpoint(args.checkpoint)
        validate_app_checkpoint(
            checkpoint,
            repository=args.repository,
            job_id=args.job_id,
            app_id=args.app_id,
            base_commit=args.base_commit,
            change_commit=args.change_commit,
        )
    except AppCheckpointError as exc:
        print(f"invalid: {exc}")
        return 1
    print(
        json.dumps(
            {
                "status": "valid",
                "schema_version": checkpoint.schema_version,
                "job_id": checkpoint.job_id,
                "base_commit": checkpoint.base_commit,
                "change_commit": checkpoint.change_commit,
                "changed_files": checkpoint.changed_files,
                "verification_count": len(checkpoint.verification),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
