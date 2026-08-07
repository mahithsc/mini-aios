from __future__ import annotations

import json
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path

import pytest

from aios_core import db
from aios_core.apps import (
    AppService,
    AppSourceError,
    AppStatus,
    ManifestValidationError,
    parse_manifest,
)


def _manifest(**overrides) -> dict:
    manifest = {
        "schemaVersion": 1,
        "name": "Example App",
        "description": "An App used by the registry tests.",
        "version": "1.0.0",
        "skills": [],
        "mcpServers": [],
        "executables": [
            {
                "id": "hello",
                "cwd": ".",
                "command": ["python", "main.py"],
                "timeoutSeconds": 30,
            }
        ],
        "prepare": [],
        "runtime": {
            "network": False,
            "persistentData": False,
            "memoryMb": 256,
            "cpus": 0.5,
            "maxProcesses": 16,
        },
    }
    manifest.update(overrides)
    return manifest


@pytest.fixture
def app_service(tmp_path: Path) -> AppService:
    return AppService(
        applications_dir=tmp_path / "workspace" / "applications",
        state_dir=tmp_path / "state",
        db_path=tmp_path / "state" / "aios.db",
    )


def _write_app(
    service: AppService,
    slug: str = "example",
    *,
    manifest: dict | None = None,
) -> Path:
    root = service.applications_dir / slug
    root.mkdir(parents=True)
    (root / "app.json").write_text(
        json.dumps(manifest or _manifest()),
        encoding="utf-8",
    )
    (root / "main.py").write_text("print('hello')\n", encoding="utf-8")
    return root


@pytest.mark.parametrize(
    "mutation, message",
    [
        (
            lambda value: value.update(
                skills=[{"id": "guide", "path": "/tmp/SKILL.md"}]
            ),
            "must be relative",
        ),
        (
            lambda value: value.update(skills=[{"id": "guide", "path": "../SKILL.md"}]),
            "cannot contain '..'",
        ),
        (
            lambda value: value["executables"][0].update(command="python main.py"),
            "must be an array",
        ),
        (
            lambda value: value.update(skills=[{"id": "hello", "path": "SKILL.md"}]),
            "duplicate component id",
        ),
    ],
)
def test_manifest_rejects_unsafe_paths_shell_commands_and_duplicate_ids(
    mutation,
    message: str,
) -> None:
    value = _manifest()
    mutation(value)

    with pytest.raises(ManifestValidationError, match=message):
        parse_manifest(value)


def test_manifest_rejects_reserved_environment_names() -> None:
    value = _manifest(
        mcpServers=[
            {
                "id": "server",
                "cwd": ".",
                "command": ["python", "server.py"],
                "env": {"AIOS_STATE_DIR": "/state"},
            }
        ]
    )

    with pytest.raises(ManifestValidationError, match="reserved by AIOS"):
        parse_manifest(value)


def test_snapshot_hash_is_deterministic_and_ignores_generated_files(
    app_service: AppService,
) -> None:
    root = _write_app(app_service)
    app = app_service.register_app("example")

    first = app_service.validate(app.id)
    (root / "node_modules" / "package").mkdir(parents=True)
    (root / "node_modules" / "package" / "index.js").write_text(
        "generated",
        encoding="utf-8",
    )
    (root / ".DS_Store").write_bytes(b"ignored")
    second = app_service.validate(app.id)

    assert second.snapshot.content_hash == first.snapshot.content_hash
    assert second.snapshot.path == first.snapshot.path
    assert second.snapshot.path != root
    assert second.snapshot.path.joinpath("main.py").read_text() == "print('hello')\n"
    assert not second.snapshot.path.joinpath("node_modules").exists()

    (root / "main.py").write_text("print('updated')\n", encoding="utf-8")
    updated = app_service.validate(app.id)

    assert updated.snapshot.content_hash != first.snapshot.content_hash
    assert first.snapshot.path.joinpath("main.py").read_text() == "print('hello')\n"


def test_create_rolls_back_new_source_when_registry_write_fails(
    tmp_path: Path,
) -> None:
    class FailingRegistry:
        def get(self, _slug: str):
            return None

        def register(self, *_args, **_kwargs):
            raise RuntimeError("database unavailable")

    service = AppService(
        registry=FailingRegistry(),
        applications_dir=tmp_path / "applications",
        state_dir=tmp_path / "state",
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        service.create_app("example")

    assert not (service.applications_dir / "example").exists()


def test_manifest_paths_must_reference_included_files(app_service: AppService) -> None:
    root = _write_app(
        app_service,
        manifest=_manifest(skills=[{"id": "guide", "path": "missing/SKILL.md"}]),
    )
    app = app_service.register_app("example")

    with pytest.raises(ManifestValidationError, match="regular file"):
        app_service.validate(app.id)

    assert root.exists()
    assert app_service.registry.require(app.id).status is AppStatus.BROKEN


@pytest.mark.parametrize("contents", [b"x" * (513 * 1024), b"\xff\xfe"])
def test_declared_skills_must_be_bounded_utf8(
    app_service: AppService,
    contents: bytes,
) -> None:
    root = _write_app(
        app_service,
        manifest=_manifest(skills=[{"id": "guide", "path": "skills/guide/SKILL.md"}]),
    )
    skill = root / "skills" / "guide" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_bytes(contents)
    app = app_service.register_app("example")

    with pytest.raises(ManifestValidationError, match="skill guide"):
        app_service.validate(app.id)

    assert app_service.registry.require(app.id).validated_hash is None


@pytest.mark.parametrize("unsafe_kind", ["symlink", "hardlink", "fifo"])
def test_snapshot_rejects_links_and_special_files(
    app_service: AppService,
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    root = _write_app(app_service)
    if unsafe_kind == "symlink":
        target = tmp_path / "outside.txt"
        target.write_text("outside", encoding="utf-8")
        (root / "unsafe").symlink_to(target)
        expected = "symlinks"
    elif unsafe_kind == "hardlink":
        os.link(root / "main.py", root / "main-link.py")
        expected = "hardlinks"
    else:
        os.mkfifo(root / "unsafe.pipe")
        expected = "special files"
    app = app_service.register_app("example")

    with pytest.raises(AppSourceError, match=expected):
        app_service.validate(app.id)

    record = app_service.registry.require(app.id)
    assert record.validated_hash is None
    assert record.last_error


def test_failed_update_preserves_active_snapshot_and_network_approval(
    app_service: AppService,
) -> None:
    root = _write_app(app_service)
    registered = app_service.register_app("example", origin="agent")
    validated = app_service.validate(registered.id)
    content_hash = validated.snapshot.content_hash
    app_service.mark_prepared(registered.id, content_hash)
    active = app_service.activate(registered.id, content_hash, enable=True)
    approved = app_service.set_network_approved(active.id, True)

    assert approved.status is AppStatus.ENABLED
    assert approved.network_approved is True

    invalid = _manifest()
    invalid["executables"][0]["command"] = "python main.py"
    (root / "app.json").write_text(json.dumps(invalid), encoding="utf-8")

    with pytest.raises(ManifestValidationError):
        app_service.validate(active.id)

    after_failure = app_service.registry.require(active.id)
    assert after_failure.validated_hash == content_hash
    assert after_failure.prepared_hash == content_hash
    assert after_failure.active_hash == content_hash
    assert after_failure.enabled is True
    assert after_failure.network_approved is True
    assert after_failure.status is AppStatus.ENABLED
    assert after_failure.last_error


def test_valid_update_preserves_old_active_snapshot_until_activation(
    app_service: AppService,
) -> None:
    root = _write_app(app_service)
    app = app_service.register_app("example")
    first = app_service.validate(app.id)
    app_service.mark_prepared(app.id, first.snapshot.content_hash)
    app_service.activate(app.id, first.snapshot.content_hash, enable=True)

    (root / "main.py").write_text("print('version two')\n", encoding="utf-8")
    second = app_service.validate(app.id)

    assert second.app.validated_hash == second.snapshot.content_hash
    assert second.app.prepared_hash is None
    assert second.app.active_hash == first.snapshot.content_hash
    assert second.app.enabled is True
    assert second.app.status is AppStatus.UPDATE_PENDING


def test_activation_prunes_versions_no_longer_referenced(
    app_service: AppService,
) -> None:
    root = _write_app(app_service)
    app = app_service.register_app("example")
    first = app_service.validate(app.id)
    first_hash = first.snapshot.content_hash
    app_service.mark_prepared(app.id, first_hash)
    app_service.activate(app.id, first_hash, enable=True)
    first_runtime = app_service.runtimes_dir / app.id / first_hash
    first_runtime.mkdir(parents=True)
    (first_runtime / "installed.txt").write_text("v1", encoding="utf-8")

    (root / "main.py").write_text("print('version two')\n", encoding="utf-8")
    second = app_service.validate(app.id)
    second_hash = second.snapshot.content_hash

    assert first.snapshot.path.exists()
    assert second.snapshot.path.exists()
    assert first_runtime.exists()

    app_service.mark_prepared(app.id, second_hash)
    app_service.activate(app.id, second_hash, enable=True)

    assert first.snapshot.path.exists()
    assert first_runtime.exists()
    assert second.snapshot.path.exists()

    (root / "main.py").write_text("print('version three')\n", encoding="utf-8")
    third = app_service.validate(app.id)

    assert not first.snapshot.path.parent.exists()
    assert not first_runtime.exists()
    assert second.snapshot.path.exists()
    assert third.snapshot.path.exists()


def test_concurrent_validation_cannot_prune_the_winning_snapshot(
    app_service: AppService,
) -> None:
    root = _write_app(app_service)
    app = app_service.register_app("example")
    second_service = AppService(
        applications_dir=app_service.applications_dir,
        state_dir=app_service.state_dir,
        db_path=app_service.registry.db_path,
    )
    recorded = threading.Event()
    release = threading.Event()
    original_record_validation = app_service.registry.record_validation

    def blocking_record_validation(*args, **kwargs):
        result = original_record_validation(*args, **kwargs)
        recorded.set()
        assert release.wait(timeout=5)
        return result

    app_service.registry.record_validation = blocking_record_validation
    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(app_service.validate, app.id)
        assert recorded.wait(timeout=5)
        (root / "main.py").write_text("print('version two')\n", encoding="utf-8")
        second_future = executor.submit(second_service.validate, app.id)
        with pytest.raises(FutureTimeoutError):
            second_future.result(timeout=0.05)
        release.set()
        first_future.result(timeout=5)
        second = second_future.result(timeout=5)

    current = app_service.registry.require(app.id)
    assert current.validated_hash == second.snapshot.content_hash
    assert second.snapshot.path.exists()


def test_manifest_network_request_does_not_approve_network(
    app_service: AppService,
) -> None:
    manifest = _manifest()
    manifest["runtime"]["network"] = True
    _write_app(app_service, manifest=manifest)
    app = app_service.register_app("example")

    validated = app_service.validate(app.id)

    assert validated.manifest.runtime.network is True
    assert validated.app.network_approved is False


def test_apps_schema_is_idempotent_and_includes_network_approval(
    tmp_path: Path,
) -> None:
    database = tmp_path / "aios.db"

    db.initialize_app_db(str(database))
    db.initialize_app_db(str(database))

    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(apps)")}
    assert {
        "id",
        "slug",
        "manifest_json",
        "validated_hash",
        "prepared_hash",
        "active_hash",
        "network_approved",
    } <= columns
