"""Authoritative evidence and final-response disclosures for deploy stubs."""

from __future__ import annotations

import ast
import json
from collections.abc import Mapping


STUB_DEPLOYMENT_DISCLOSURE = (
    "This was an orchestration simulation. No artifact was created, uploaded, "
    "or verified, and no deployment or live route was created. The temporary "
    "Codex worktree remains allocated because cleanup was not performed."
)


def stub_deployment_evidence(*, worktree_path: str | None = None) -> dict:
    """Return explicit negative evidence that must override stub status labels."""

    evidence = {
        "artifact_created": False,
        "artifact_uploaded": False,
        "artifact_verified": False,
        "worktree_removed": False,
        "deployment_performed": False,
        "route_live": False,
        "required_disclosure": STUB_DEPLOYMENT_DISCLOSURE,
    }
    if worktree_path:
        evidence["worktree_path"] = worktree_path
    return evidence


def required_disclosures_from_tool_result(result: object) -> list[str]:
    """Extract model-independent disclosure text from a structured tool result."""

    payload: object = result
    if isinstance(result, str):
        try:
            payload = json.loads(result)
        except json.JSONDecodeError:
            try:
                payload = ast.literal_eval(result)
            except (SyntaxError, ValueError):
                return []
    if not isinstance(payload, Mapping):
        return []

    disclosure = payload.get("required_disclosure")
    if isinstance(disclosure, str) and disclosure.strip():
        return [disclosure.strip()]
    if isinstance(disclosure, list):
        return [
            item.strip()
            for item in disclosure
            if isinstance(item, str) and item.strip()
        ]
    return []


def missing_disclosure_suffix(text: str, disclosures: list[str]) -> str:
    """Build the exact final text that the runtime must append, without duplicates."""

    missing = []
    for disclosure in disclosures:
        if disclosure not in text and disclosure not in missing:
            missing.append(disclosure)
    if not missing:
        return ""
    prefix = "\n\n" if text else ""
    return prefix + "\n\n".join(missing)
