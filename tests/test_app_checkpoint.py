from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from aios_core.app_checkpoint import (
    AppCheckpointError,
    load_app_checkpoint,
    main,
    validate_app_checkpoint,
)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str, str, list[str]]:
    root = tmp_path / "app"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "AIOS Test")
    _git(root, "config", "user.email", "aios@example.test")
    (root / "source.txt").write_text("blue\n")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "baseline")
    base = _git(root, "rev-parse", "HEAD")
    (root / "source.txt").write_text("green\n")
    (root / "HISTORY.md").write_text("Changed source.\n")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "change")
    change = _git(root, "rev-parse", "HEAD")
    files = _git(root, "diff", "--name-only", base, change).splitlines()
    return root, base, change, files


def _payload(base: str, change: str, files: list[str]) -> dict:
    return {
        "schema_version": 1,
        "job_id": "job-123",
        "app_id": "app_123",
        "base_commit": base,
        "change_commit": change,
        "summary": "Changed source",
        "changed_files": files,
        "verification": ["tests passed"],
    }


def test_shared_checkpoint_schema_and_git_preflight(tmp_path: Path) -> None:
    root, base, change, files = _repository(tmp_path)
    checkpoint_path = root / "checkpoint.json"
    checkpoint_path.write_text(json.dumps(_payload(base, change, files)))

    checkpoint = load_app_checkpoint(checkpoint_path)
    result = validate_app_checkpoint(
        checkpoint,
        repository=root,
        job_id="job-123",
        app_id="app_123",
        base_commit=base,
        change_commit=change,
    )

    assert result.schema_version == 1
    assert result.verification == ["tests passed"]


@pytest.mark.parametrize(
    "mutation, expected",
    [
        (lambda payload: payload.update(schema_version=2), "schema_version"),
        (
            lambda payload: payload.update(
                codex_verification=payload.pop("verification")
            ),
            "verification",
        ),
        (lambda payload: payload.update(verification="tests passed"), "verification"),
        (lambda payload: payload.update(extra_field=True), "extra_field"),
    ],
)
def test_checkpoint_schema_rejects_variants(
    tmp_path: Path, mutation, expected: str
) -> None:
    root, base, change, files = _repository(tmp_path)
    payload = _payload(base, change, files)
    mutation(payload)
    checkpoint_path = root / "checkpoint.json"
    checkpoint_path.write_text(json.dumps(payload))

    with pytest.raises(AppCheckpointError, match=expected):
        load_app_checkpoint(checkpoint_path)


def test_preflight_cli_uses_the_same_schema(tmp_path: Path, capsys) -> None:
    root, base, change, files = _repository(tmp_path)
    checkpoint_path = root / "checkpoint.json"
    checkpoint_path.write_text(json.dumps(_payload(base, change, files)))

    exit_code = main(
        [
            "validate",
            "--checkpoint",
            str(checkpoint_path),
            "--repository",
            str(root),
            "--job-id",
            "job-123",
            "--app-id",
            "app_123",
            "--base-commit",
            base,
            "--change-commit",
            change,
        ]
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["status"] == "valid"
