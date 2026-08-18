from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
_PLATFORMS = {"linux-amd64", "linux-arm64"}
_DATABASE_SCHEMA_VERSION = 6


def _write_new(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def generate_keypair(private_path: Path, public_path: Path) -> str:
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    _write_new(private_path, private_pem)
    _write_new(public_path, public_pem, 0o644)
    return key_id(public_key)


def key_id(public_key: Ed25519PublicKey) -> str:
    raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return hashlib.sha256(raw).hexdigest()[:16]


def _parse_timestamp(value: str, field: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include timezone information")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_artifact(value: str) -> tuple[str, dict[str, object]]:
    try:
        platform, image_and_size = value.split("=", 1)
        image, size_text = image_and_size.rsplit(":", 1)
        repository, digest = image.rsplit("@", 1)
        size = int(size_text)
    except (ValueError, TypeError) as exc:
        raise ValueError(
            "artifact must be PLATFORM=REPOSITORY@sha256:DIGEST:SIZE_BYTES"
        ) from exc
    if platform not in _PLATFORMS:
        raise ValueError(f"unsupported artifact platform {platform!r}")
    digest = "sha256:" + digest.removeprefix("sha256:")
    if not repository or not _DIGEST.fullmatch(digest):
        raise ValueError("artifact repository or digest is invalid")
    if size < 0:
        raise ValueError("artifact size cannot be negative")
    return platform, {
        "repository": repository,
        "digest": digest,
        "sizeBytes": size,
    }


def build_manifest(arguments: argparse.Namespace) -> dict[str, object]:
    artifacts = dict(parse_artifact(value) for value in arguments.artifact)
    if not artifacts:
        raise ValueError("at least one artifact is required")
    if arguments.from_schema_minimum > arguments.from_schema_maximum:
        raise ValueError("database schema minimum exceeds maximum")
    manifest: dict[str, object] = {
        "schemaVersion": 1,
        "product": "mini-aios",
        "releaseId": arguments.release_id,
        "version": arguments.version,
        "sequence": arguments.sequence,
        "channel": arguments.channel,
        "publishedAt": _parse_timestamp(arguments.published_at, "publishedAt"),
        "expiresAt": _parse_timestamp(arguments.expires_at, "expiresAt"),
        "minimumUpdaterVersion": arguments.minimum_updater_version,
        "artifacts": artifacts,
        "database": {
            "fromSchemaMinimum": arguments.from_schema_minimum,
            "fromSchemaMaximum": arguments.from_schema_maximum,
            "toSchema": arguments.to_schema,
            "previousAppCanReadToSchema": arguments.previous_app_can_read,
            "restoreBackupOnRollback": arguments.restore_backup_on_rollback,
            "destructive": arguments.destructive,
        },
        "policy": {
            "critical": arguments.critical,
            "allowForcedDrain": arguments.allow_forced_drain,
            "drainTimeoutSeconds": arguments.drain_timeout,
            "startupTimeoutSeconds": arguments.startup_timeout,
            "observationSeconds": arguments.observation,
            "consecutiveHealthFailureLimit": arguments.health_failure_limit,
        },
    }
    if arguments.release_notes_url:
        manifest["releaseNotesUrl"] = arguments.release_notes_url
    if arguments.revision:
        manifest["revision"] = arguments.revision
    return manifest


def sign_manifest(manifest_path: Path, private_key_path: Path) -> dict[str, object]:
    payload = manifest_path.read_bytes()
    decoded = json.loads(payload)
    if decoded.get("product") != "mini-aios" or decoded.get("schemaVersion") != 1:
        raise ValueError("manifest product/schema is invalid")
    private_key = serialization.load_pem_private_key(
        private_key_path.read_bytes(),
        password=None,
    )
    if not isinstance(private_key, Ed25519PrivateKey):
        raise ValueError("release private key must be Ed25519")
    signature = private_key.sign(payload)
    return {
        "formatVersion": 1,
        "keyId": key_id(private_key.public_key()),
        "payload": base64.b64encode(payload).decode("ascii"),
        "signature": base64.b64encode(signature).decode("ascii"),
    }


def verify_envelope(envelope_path: Path, public_key_path: Path) -> dict[str, object]:
    envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
    if envelope.get("formatVersion") != 1:
        raise ValueError("unsupported envelope format")
    payload = base64.b64decode(envelope["payload"], validate=True)
    signature = base64.b64decode(envelope["signature"], validate=True)
    public_key = serialization.load_pem_public_key(public_key_path.read_bytes())
    if not isinstance(public_key, Ed25519PublicKey):
        raise ValueError("release public key must be Ed25519")
    public_key.verify(signature, payload)
    if envelope.get("keyId") != key_id(public_key):
        raise ValueError("envelope key ID does not match public key")
    return json.loads(payload)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Publish signed Mini AIOS updates")
    subparsers = parser.add_subparsers(dest="command", required=True)

    keygen = subparsers.add_parser("keygen")
    keygen.add_argument("--private-key", type=Path, required=True)
    keygen.add_argument("--public-key", type=Path, required=True)

    create = subparsers.add_parser("create-manifest")
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--release-id", required=True)
    create.add_argument("--version", required=True)
    create.add_argument("--sequence", required=True, type=int)
    create.add_argument(
        "--channel", choices=("dev", "beta", "stable", "pinned"), default="stable"
    )
    create.add_argument("--published-at", required=True)
    create.add_argument("--expires-at", required=True)
    create.add_argument("--minimum-updater-version", default="0.1.0")
    create.add_argument("--release-notes-url", default="")
    create.add_argument("--revision", default="")
    create.add_argument("--artifact", action="append", default=[])
    create.add_argument("--from-schema-minimum", type=int, default=1)
    create.add_argument(
        "--from-schema-maximum", type=int, default=_DATABASE_SCHEMA_VERSION
    )
    create.add_argument("--to-schema", type=int, default=_DATABASE_SCHEMA_VERSION)
    create.add_argument(
        "--previous-app-can-read", action=argparse.BooleanOptionalAction, default=True
    )
    create.add_argument(
        "--restore-backup-on-rollback",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    create.add_argument("--destructive", action="store_true")
    create.add_argument("--critical", action="store_true")
    create.add_argument("--allow-forced-drain", action="store_true")
    create.add_argument("--drain-timeout", type=int, default=300)
    create.add_argument("--startup-timeout", type=int, default=120)
    create.add_argument("--observation", type=int, default=300)
    create.add_argument("--health-failure-limit", type=int, default=3)

    sign = subparsers.add_parser("sign")
    sign.add_argument("--manifest", type=Path, required=True)
    sign.add_argument("--private-key", type=Path, required=True)
    sign.add_argument("--output", type=Path, required=True)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--envelope", type=Path, required=True)
    verify.add_argument("--public-key", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "keygen":
            generated_id = generate_keypair(arguments.private_key, arguments.public_key)
            print(generated_id)
        elif arguments.command == "create-manifest":
            manifest = build_manifest(arguments)
            arguments.output.parent.mkdir(parents=True, exist_ok=True)
            arguments.output.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        elif arguments.command == "sign":
            envelope = sign_manifest(arguments.manifest, arguments.private_key)
            arguments.output.parent.mkdir(parents=True, exist_ok=True)
            arguments.output.write_text(
                json.dumps(envelope, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        elif arguments.command == "verify":
            manifest = verify_envelope(arguments.envelope, arguments.public_key)
            print(json.dumps(manifest, indent=2, sort_keys=True))
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
