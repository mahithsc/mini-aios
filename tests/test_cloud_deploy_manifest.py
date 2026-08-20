from __future__ import annotations

import tarfile
from pathlib import Path

import pytest

from aios_core.deploy.archive import create_artifact_archive
from aios_core.deploy.manifest import (
    ManifestValidationError,
    load_deployment_manifest,
    validate_artifact_tree,
    validate_cloud_artifact,
)


def _write_valid_app(root: Path) -> None:
    (root / "database" / "migrations").mkdir(parents=True)
    (root / "database" / "migrations" / "001_create.sql").write_text(
        "create table recipes (id bigint primary key);\n"
    )
    (root / "server").mkdir()
    (root / "server" / "Dockerfile").write_text(
        'FROM python:3.12-slim\nCMD ["python", "app.py"]\n'
    )
    (root / "server" / "app.py").write_text("print('ready')\n")
    (root / ".env.example").write_text("OPENAI_API_KEY=\n")
    (root / "aios.deploy.yaml").write_text(
        """
version: 1
app_id: app_abc123
database:
  migrations: database/migrations
  required_extensions:
    - vector
server:
  source: server
  dockerfile: server/Dockerfile
  health_path: /health
  secrets:
    - env: OPENAI_API_KEY
      secret_ref: sec_primary
      exposure: runtime
""".lstrip()
    )


def test_valid_manifest_and_artifact_are_deterministic(tmp_path: Path) -> None:
    _write_valid_app(tmp_path)

    manifest, first = validate_cloud_artifact(tmp_path)
    second = validate_artifact_tree(tmp_path)

    assert manifest.app_id == "app_abc123"
    assert manifest.database is not None
    assert manifest.database.required_extensions == ["vector"]
    assert manifest.server is not None
    assert manifest.server.secrets[0].secret_ref == "sec_primary"
    assert first == second
    assert first.file_count == 5

    (tmp_path / "server" / "app.py").write_text("print('changed')\n")
    assert validate_artifact_tree(tmp_path).sha256 != first.sha256


@pytest.mark.parametrize(
    "manifest",
    [
        "version: 2\napp_id: app_abc\nserver:\n  source: server\n",
        "version: 1\napp_id: app_abc\n",
        (
            "version: 1\napp_id: app_abc\nserver:\n"
            "  source: ../outside\n  dockerfile: server/Dockerfile\n"
        ),
        (
            "version: 1\napp_id: app_abc\nserver:\n  source: server\n"
            "  secrets:\n    - env: TOKEN\n      secret_ref: sec_ok\n"
            "      value: must-not-be-here\n"
        ),
        (
            "version: 1\napp_id: app_abc\nserver:\n  source: server\n"
            "  secrets:\n    - env: TOKEN\n      secret_ref: sec_ok\n"
            "      exposure: build\n"
        ),
        (
            "version: 1\napp_id: app_abc\nserver:\n  source: server\n"
            "  secrets:\n    - env: PORT\n      secret_ref: sec_ok\n"
        ),
    ],
)
def test_invalid_manifest_contract_is_rejected(tmp_path: Path, manifest: str) -> None:
    (tmp_path / "server").mkdir()
    (tmp_path / "server" / "Dockerfile").write_text("FROM scratch\n")
    (tmp_path / "aios.deploy.yaml").write_text(manifest)

    with pytest.raises(ManifestValidationError):
        load_deployment_manifest(tmp_path)


@pytest.mark.parametrize(
    ("relative", "contents"),
    [
        (".env.production", "OPENAI_API_KEY=secret\n"),
        ("server/private.pem", "-----BEGIN PRIVATE KEY-----\nsecret\n"),
        ("server/config.py", 'TOKEN = "sk_live_1234567890abcdefgh"\n'),
    ],
)
def test_artifact_rejects_secret_material(
    tmp_path: Path,
    relative: str,
    contents: str,
) -> None:
    _write_valid_app(tmp_path)
    target = tmp_path / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(contents)

    with pytest.raises(ManifestValidationError):
        validate_artifact_tree(tmp_path)


def test_artifact_rejects_symlinks(tmp_path: Path) -> None:
    _write_valid_app(tmp_path)
    (tmp_path / "server" / "linked").symlink_to(tmp_path / "server" / "app.py")

    with pytest.raises(ManifestValidationError, match="Symbolic links"):
        validate_artifact_tree(tmp_path)


def test_archive_contains_dockerfile_and_is_deterministic(tmp_path: Path) -> None:
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    _write_valid_app(app_dir)

    first = create_artifact_archive(app_dir, tmp_path / "first.tar.gz")
    second = create_artifact_archive(app_dir, tmp_path / "second.tar.gz")

    assert first.sha256 == second.sha256
    assert first.size == second.size
    assert first.source.file_count == 5
    with tarfile.open(first.path, "r:gz") as archive:
        members = archive.getnames()
        dockerfile = archive.extractfile("server/Dockerfile")
        assert dockerfile is not None
        assert dockerfile.read().startswith(b"FROM python:3.12-slim")
    assert members == sorted(members)
    assert ".env.example" in members
    assert "server/Dockerfile" in members


def test_artifact_excludes_aios_history_and_handoff_metadata(tmp_path: Path) -> None:
    _write_valid_app(tmp_path)
    (tmp_path / "HISTORY.md").write_text("human history\n")
    handoff = tmp_path / ".aios" / "handoffs" / "job-1.json"
    handoff.parent.mkdir(parents=True)
    handoff.write_text('{"job_id":"job-1"}\n')

    archive = create_artifact_archive(tmp_path, tmp_path.parent / "artifact.tar.gz")

    with tarfile.open(archive.path, "r:gz") as tar:
        members = tar.getnames()
    assert "HISTORY.md" not in members
    assert not any(member.startswith(".aios/") for member in members)
