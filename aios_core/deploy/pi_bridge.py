"""One-shot deployment bridge for the trusted Pi ``deploy`` extension.

The bridge deliberately accepts only an application slug.  The source directory
is always the process working directory, which the Pi extension sets to the
session's ``ctx.cwd``.  Keeping the path out of the request prevents the coding
agent from asking the privileged deployer to mount an unrelated host directory.

Protocol:

    python pi_bridge.py --slug my-app

Exactly one JSON object is written to stdout.  A valid deployment attempt exits
zero even when the application itself fails to become healthy; that expected
failure is represented by ``{"status": "error", ...}`` so Pi can read the logs,
fix the application, and retry.  Invalid requests and bridge failures use a
non-zero exit status while still returning structured JSON.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Callable

# The extension invokes this file by absolute path so it also works when
# mini-aios is not installed as a site package.  Bootstrap the repository root
# only for that direct-script entry point; normal module imports are unchanged.
if __package__ in {None, ""}:  # pragma: no cover - exercised by subprocess test
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from aios_core.deploy.deployer import deploy as _deploy
from aios_core.deploy.store import ProjectStore


BRIDGE_VERSION = 1
_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class BridgeRequestError(ValueError):
    """The extension sent a request that the bridge will not execute."""


def validate_slug(slug: str) -> str:
    """Return a Docker/DNS-safe application slug or raise a request error."""

    if not isinstance(slug, str) or not _SLUG_RE.fullmatch(slug):
        raise BridgeRequestError(
            "slug must be 1-63 lowercase letters, digits, or hyphens, "
            "and must start and end with a letter or digit"
        )
    return slug


def _parse_request(argv: Sequence[str]) -> str:
    # A deliberately tiny CLI surface: there is no source-dir option and no
    # positional path that could be confused for one.
    if len(argv) != 2 or argv[0] != "--slug":
        raise BridgeRequestError("usage: pi_bridge.py --slug <app-slug>")
    return validate_slug(argv[1])


def deploy_from_cwd(
    slug: str,
    *,
    _cwd: Path | None = None,
    _deploy_fn: Callable[..., dict[str, Any]] | None = None,
    _store_factory: Callable[[], ProjectStore] | None = None,
) -> dict[str, Any]:
    """Deploy ``slug`` using only the bridge process's current directory.

    Underscored keyword arguments are dependency-injection seams for unit tests;
    the command-line protocol cannot set them.
    """

    safe_slug = validate_slug(slug)
    source_dir = (_cwd if _cwd is not None else Path.cwd()).resolve(strict=True)
    if not source_dir.is_dir():
        raise BridgeRequestError("the Pi working directory is not a directory")

    deploy_fn = _deploy_fn or _deploy
    store_factory = _store_factory or ProjectStore
    result = deploy_fn(safe_slug, source_dir, store=store_factory())
    if not isinstance(result, dict) or not isinstance(result.get("status"), str):
        raise RuntimeError("deployer returned an invalid response")

    # Source and slug are bridge-owned metadata.  Write them after the deployer
    # payload so a future deployer cannot accidentally override the trusted
    # values.
    return {
        **result,
        "slug": safe_slug,
        "source_dir": str(source_dir),
        "bridge_version": BRIDGE_VERSION,
    }


def _error_payload(code: str, message: str) -> dict[str, Any]:
    return {
        "status": "error",
        "error_code": code,
        "error": message,
        "bridge_version": BRIDGE_VERSION,
    }


def _write_payload(payload: dict[str, Any]) -> None:
    # Compact, single-line JSON keeps the stdout contract unambiguous.
    sys.stdout.write(json.dumps(payload, separators=(",", ":"), default=str) + "\n")
    sys.stdout.flush()


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        slug = _parse_request(args)
    except BridgeRequestError as exc:
        _write_payload(_error_payload("invalid_request", str(exc)))
        return 2

    try:
        result = deploy_from_cwd(slug)
    except BridgeRequestError as exc:
        _write_payload(_error_payload("invalid_request", str(exc)))
        return 2
    except Exception as exc:
        # Expected build/health failures are already returned by deploy().  This
        # branch represents a broken bridge or host and must not leak a traceback
        # into the JSONL/RPC stream.
        message = str(exc).strip() or type(exc).__name__
        _write_payload(_error_payload("bridge_failure", f"deploy bridge failed: {message}"))
        return 1

    _write_payload(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
