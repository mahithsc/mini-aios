from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidSignature

from release.publish_update import (
    build_manifest,
    generate_keypair,
    sign_manifest,
    verify_envelope,
)


def _arguments() -> argparse.Namespace:
    now = datetime.now(timezone.utc)
    return argparse.Namespace(
        release_id="release-2",
        version="0.2.0",
        sequence=2,
        channel="stable",
        published_at=now.isoformat(),
        expires_at=(now + timedelta(days=30)).isoformat(),
        minimum_updater_version="0.1.0",
        release_notes_url="https://github.com/mahithsc/mini-aios/releases/tag/v0.2.0",
        revision="abc123",
        artifact=[
            "linux-arm64=ghcr.io/mahithsc/mini-aios@sha256:"
            + "a" * 64
            + ":0"
        ],
        from_schema_minimum=1,
        from_schema_maximum=1,
        to_schema=1,
        previous_app_can_read=True,
        restore_backup_on_rollback=True,
        destructive=False,
        critical=False,
        allow_forced_drain=False,
        drain_timeout=300,
        startup_timeout=120,
        observation=300,
        health_failure_limit=3,
    )


def test_publisher_signs_and_verifies_exact_manifest(tmp_path: Path) -> None:
    private_key = tmp_path / "private.pem"
    public_key = tmp_path / "public.pem"
    generate_keypair(private_key, public_key)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(build_manifest(_arguments())), encoding="utf-8")
    envelope_path = tmp_path / "stable.json"
    envelope_path.write_text(
        json.dumps(sign_manifest(manifest_path, private_key)),
        encoding="utf-8",
    )

    verified = verify_envelope(envelope_path, public_key)
    assert verified["releaseId"] == "release-2"
    assert verified["artifacts"]["linux-arm64"]["digest"] == "sha256:" + "a" * 64

    payload = json.loads(envelope_path.read_text(encoding="utf-8"))
    payload["signature"] = payload["signature"][:-2] + "AA"
    envelope_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises((InvalidSignature, ValueError)):
        verify_envelope(envelope_path, public_key)


def test_key_generation_refuses_to_overwrite(tmp_path: Path) -> None:
    private_key = tmp_path / "private.pem"
    public_key = tmp_path / "public.pem"
    generate_keypair(private_key, public_key)
    with pytest.raises(FileExistsError):
        generate_keypair(private_key, public_key)
