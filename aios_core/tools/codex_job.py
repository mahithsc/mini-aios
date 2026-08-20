"""Interactive Codex jobs backed by ``codex app-server``.

The app-server protocol is bidirectional JSON-RPC. That matters because Codex
can pause a turn with ``item/tool/requestUserInput``; the old ``codex exec``
wrapper had no response channel and treated that pause as a subprocess error.

The public start/poll/stop API remains stable. Polling may now return
``status="awaiting_input"`` and a structured ``pending_input``. Answers can be
submitted either through :func:`codex_answer` or the gateway route used by the
desktop client.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shlex
import signal
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from time import monotonic, sleep
from typing import Any
from uuid import uuid4

from ..app_git import (
    AppGitError,
    inspect_app_repository,
    read_blob,
    resolve_commit,
    resolve_tree,
    run_git,
    validate_change_topology,
    validate_source_range,
)
from ..app_checkpoint import (
    AppCheckpoint,
    AppCheckpointError,
    load_app_checkpoint,
    validate_app_checkpoint,
)
from ..deploy.manifest import (
    ManifestValidationError,
    find_deployment_root,
    load_deployment_manifest,
)
from ..deploy.worktree_handoff import (
    WorktreeHandoffError,
    WorktreeRecord,
    WorktreeRegistry,
    WorktreeStatus,
)
from ..runtime_context import (
    get_current_chat_id,
    get_current_run_id,
    resolve_codex_workdir,
)
from ..workspace import get_workspace_dir
from .codex_run_store import CodexRunStore
from .codex_subagent import translate_codex_event


# Compatibility seam retained for tests and callers that previously patched
# this module-level resolver.
def resolve_chat_files_path(path: str) -> Path:
    return resolve_codex_workdir(path)


_REPO_ROOT = str(Path(__file__).resolve().parents[2])
_ProgressSink = Callable[[str, str, dict[str, Any]], None]
_progress_sink: _ProgressSink | None = None
_LifecycleSink = Callable[[str, str, str], None]
_lifecycle_sink: _LifecycleSink | None = None
_UnexpectedExitSink = Callable[["CodexJob", str], None]
log = logging.getLogger(__name__)


def set_progress_sink(sink: _ProgressSink | None) -> None:
    global _progress_sink
    _progress_sink = sink


def set_lifecycle_sink(sink: _LifecycleSink | None) -> None:
    global _lifecycle_sink
    _lifecycle_sink = sink


def _deploy_mcp_config() -> str:
    return (
        'mcp_servers.deploy={command="' + sys.executable + '",'
        'args=["-m","aios_core.deploy.mcp_server"],'
        'env={PYTHONPATH="' + _REPO_ROOT + '"}}'
    )


SAFETY_CAP_SECONDS = float(os.getenv("AIOS_CODEX_SAFETY_CAP", "1800"))
MAX_ACTIVE_JOBS = int(os.getenv("AIOS_CODEX_MAX_JOBS", "6"))
RPC_TIMEOUT_SECONDS = float(os.getenv("AIOS_CODEX_RPC_TIMEOUT", "30"))
MAX_EVENT_TEXT_CHARS = int(os.getenv("AIOS_CODEX_EVENT_TEXT_LIMIT", "50000"))
MAX_STDERR_CHARS = int(os.getenv("AIOS_CODEX_STDERR_LIMIT", "100000"))
RETENTION_DAYS = int(os.getenv("AIOS_CODEX_RETENTION_DAYS", "30"))
MAX_RECOVERY_ATTEMPTS = int(os.getenv("AIOS_CODEX_MAX_RECOVERIES", "2"))
MAX_DEPLOY_FOLLOWUPS = int(os.getenv("AIOS_CODEX_MAX_DEPLOY_FOLLOWUPS", "2"))
MAX_APP_HANDOFF_FOLLOWUPS = int(os.getenv("AIOS_CODEX_MAX_HANDOFF_FOLLOWUPS", "2"))
RECOVERY_BACKOFF_BASE_SECONDS = float(
    os.getenv("AIOS_CODEX_RECOVERY_BACKOFF_BASE", "0.5")
)
_ACTIVE_STATUSES = {"running", "awaiting_input"}
_TERMINAL_STATUSES = {"done", "error", "cancelled"}
_DEPLOY_TOOL_BY_COMPONENT = {
    "database": "deploy_database",
    "server": "deploy_server",
    "frontend": "deploy_frontend",
}


def _deployment_contract_task(task: str) -> str:
    """Attach the non-optional contract for a deploy-enabled Codex run."""

    return f"""{task.strip()}

AIOS CLOUD DEPLOYMENT CONTRACT (MANDATORY)
The user requested a real cloud deployment; building files alone is not completion.
1. Work from the app root containing aios.deploy.yaml. Create or correct that manifest so it declares every component this task actually builds.
2. Use only the tools on the `deploy` MCP server for deployment. Never use built-in hosting/deployment tools or provider CLIs.
3. Call every matching tool for every declared component, in dependency order: deploy_database, then deploy_server, then deploy_frontend. Skip only components that are absent from the manifest. A dependent component must not be enqueued merely because its prerequisite returned a deployment ID: wait until the database is active before calling deploy_server, and wait until the server is active before calling deploy_frontend.
4. A queued/building response with a deployment ID proves only that the deployment was created, not that its dependents are ready. Call check_app_status with the manifest app_id to inspect every component, its queue/build/failure state, latest event, and artifact upload/verification state. Use get_deployment_status and get_deployment_events for deeper per-job diagnostics. If a prerequisite fails or requires user action, report that state and do not enqueue its dependents. Do not wait forever on infrastructure that remains queued.
5. If a deploy call returns an actionable artifact/manifest validation error, fix the artifact and retry the same deploy tool. Treat secrets as blockers only when the AIOS tool itself returns awaiting_secrets; do not invent a secret requirement.
6. Do not finish with 'deployment was not performed.' Before finishing, report each deploy tool called, its deployment ID, and its current status. If an AIOS tool still cannot create a deployment, report its exact response.
"""


def _checkpoint_contract_block(contract: dict[str, Any]) -> str:
    """Render the canonical model schema and pre-commit validator for Codex."""

    job_id = str(contract["job_id"])
    app_id = str(contract["app_id"])
    app_root = str(contract["app_root"])
    schema = json.dumps(AppCheckpoint.model_json_schema(), indent=2, sort_keys=True)
    example = json.dumps(
        {
            "schema_version": 1,
            "job_id": job_id,
            "app_id": app_id,
            "base_commit": "FULL_40_CHARACTER_B_OID",
            "change_commit": "FULL_40_CHARACTER_C_OID",
            "summary": "Concise substantive change summary",
            "changed_files": ["exact/path/from/B-to-C-diff"],
            "verification": ["Concrete verification command and result"],
        },
        indent=2,
    )
    validator = " ".join(
        [
            f"PYTHONPATH={shlex.quote(_REPO_ROOT)}",
            shlex.quote(sys.executable),
            "-m aios_core.app_checkpoint validate",
            f"--checkpoint {shlex.quote(f'.aios/checkpoints/{job_id}.json')}",
            f"--repository {shlex.quote(app_root)}",
            f"--job-id {shlex.quote(job_id)}",
            f"--app-id {shlex.quote(app_id)}",
            "--base-commit FULL_B_OID",
            "--change-commit FULL_C_OID",
        ]
    )
    return f"""
EXACT AIOS CHECKPOINT SCHEMA — NO VARIANTS:
The checkpoint must be valid JSON with exactly the eight keys shown below. No
aliases or extra keys are accepted. `schema_version` is the integer 1.
`base_commit` and `change_commit` are full lowercase 40-character Git OIDs.
`changed_files` is the exact non-empty B-to-C file list. `verification` is a
non-empty JSON array of non-empty strings; never use `codex_verification`.

Canonical example:
{example}

The host validates with this generated JSON Schema from the same typed model:
{schema}

Before committing M, replace FULL_B_OID and FULL_C_OID and run this exact
preflight command from the canonical app repository. Do not commit M unless it
prints `status: valid` JSON:
{validator}
"""


def _app_change_contract_task_v2(task: str, contract: dict[str, Any]) -> str:
    """Attach the v2 Codex commit/history/workspace handoff contract."""

    base = contract.get("base_commit") or "<bootstrap-baseline-created-by-codex>"
    checkpoint_contract = _checkpoint_contract_block(contract)
    return f"""{task.strip()}

AIOS APP CHANGE AND WORKSPACE HANDOFF CONTRACT v2 (MANDATORY)
You prepare source; you do not deploy it. Do not call cloud deployment tools or provider CLIs.

Identity and reserved handoff:
- job_id: {contract["job_id"]}
- app_id: {contract["app_id"]}
- canonical app repository: {contract["app_root"]}
- captured branch: {contract["branch"]}
- captured base B: {base}
- worktree_id: {contract["worktree_id"]}
- reserved detached worktree path: {contract["workspace_path"]}

The canonical live app repository must be clean when you finish. Never stash, reset, clean, switch branches, fetch, push, or rewrite its existing history during the initial turn. Only a later host repair turn may explicitly authorize replacing the invalid, unpushed metadata suffix created by this same job; it may never rewrite B, C, or earlier history.

Choose exactly one completion mode:

MODE `change` — use when this task changes app source:
1. If captured base is the bootstrap placeholder, create one baseline root commit B from the untouched existing app files before making task changes. Otherwise B is the captured base above.
2. Make the requested change. Update README.md only when behavior, setup, recovery, or structure changed. Keep aios.deploy.yaml accurate and store only environment names and secret-reference IDs, never secret values.
3. Append a timestamped human narrative to HISTORY.md describing the major change and verification. Create substantive single-parent commit C. C's parent must be B.
4. Resolve C's full commit and tree OIDs. Append a checkpoint entry to HISTORY.md containing the full C OID and write .aios/checkpoints/{contract["job_id"]}.json using the exact checkpoint schema and successful preflight below. Create metadata-only single-parent commit M. M may change only HISTORY.md and that exact checkpoint file, and its parent must be C. Leave the canonical app on the captured branch at clean HEAD=M.
5. Create the reserved workspace with: git worktree add --detach {contract["workspace_path"]} <full-C-oid>

MODE `selected_commit` — use only when the user asked to redeploy a historical state:
1. Do not change or commit canonical source. Search HISTORY.md, checkpoints, git log, and diffs for the requested change.
2. Select the exact full commit C representing that state. If multiple commits plausibly match, stop and report candidates instead of preparing a handoff.
3. Leave the canonical repository unchanged and clean, then create the reserved detached worktree at C.

For either mode, write {contract["workspace_path"]}/.aios/CODEX_HANDOFF.json as valid JSON with exactly these fields:
{{
  "schema_version": 1,
  "job_id": "{contract["job_id"]}",
  "app_id": "{contract["app_id"]}",
  "mode": "change or selected_commit",
  "worktree_id": "{contract["worktree_id"]}",
  "canonical_repository": "{contract["app_root"]}",
  "workspace_path": "{contract["workspace_path"]}",
  "base_commit": "full B OID",
  "source_commit": "full C OID",
  "source_tree": "full C tree OID",
  "provenance_commit": "full M OID for change mode, otherwise null",
  "selection_reason": "concise evidence for why this source state matches"
}}

Do not put build output, caches, dependencies, or any other uncommitted files in the detached worktree. After writing the descriptor, do not modify or remove that worktree. Report the mode, full B/C/M OIDs as applicable, selection evidence, worktree_id, and path.

{checkpoint_contract}
"""


def _app_record_contract_task_v2(task: str, contract: dict[str, Any]) -> str:
    """Attach the app history/commit contract without requesting deployment."""

    base = contract.get("base_commit") or "<bootstrap-baseline-created-by-codex>"
    checkpoint_contract = _checkpoint_contract_block(contract)
    return f"""{task.strip()}

AIOS APP CHANGE RECORD CONTRACT v2 (MANDATORY)
This is a durable AIOS app repository. You own its local commits; do not deploy, fetch, push, switch branches, rewrite history, stash, reset, or clean it during the initial turn. Only a later host repair turn may explicitly authorize replacing the invalid, unpushed metadata suffix created by this same job; it may never rewrite B, C, or earlier history.

- job_id: {contract["job_id"]}
- app_id: {contract["app_id"]}
- canonical app repository: {contract["app_root"]}
- captured branch: {contract["branch"]}
- captured base B: {base}

If this is a read-only search, explanation, or review, leave the canonical repository exactly unchanged and clean. If this repository was just initialized, first create a baseline root commit B containing the untouched app files, even for a read-only task.

If you make any material app change:
1. If captured base is the bootstrap placeholder, create baseline B from the untouched files before editing. Otherwise use the captured B above.
2. Make the requested change. Update README.md only when behavior, setup, recovery, or structure changed. Keep aios.deploy.yaml accurate and store only secret-reference IDs/environment names, never secret values.
3. Append a UTC ISO-8601 timestamped narrative to HISTORY.md covering the major change, important files, and verification. Create substantive single-parent commit C whose parent is B.
4. Resolve C's full OID. Append a `Checkpoint recorded` HISTORY.md entry containing the full C OID and write .aios/checkpoints/{contract["job_id"]}.json using the exact checkpoint schema and successful preflight below.
5. Create metadata-only single-parent commit M. M's parent must be C and M may change only HISTORY.md and that exact checkpoint file.
6. Finish on the captured branch at clean HEAD=M. Report full B/C/M OIDs and verification.

{checkpoint_contract}
"""


def _dirty_recovery_instructions(contract: dict[str, Any]) -> str:
    if not contract.get("initial_dirty"):
        return "The canonical app was clean when this job started."
    return f"""The canonical app already contained unfinished changes when this job started:
{contract.get("initial_status") or "(status unavailable)"}
These changes are durable app work that you own. Inspect them, incorporate them into this task, verify them, and commit them in C. Do not discard, stash, reset, clean, or ask the user about Git state."""


def _app_change_contract_task(task: str, contract: dict[str, Any]) -> str:
    """Attach the v3 B-to-C source and detached-workspace contract."""

    base = contract.get("base_commit") or "<bootstrap-baseline-created-by-codex>"
    dirty = _dirty_recovery_instructions(contract)
    return f"""{task.strip()}

AIOS APP CHANGE AND WORKSPACE HANDOFF CONTRACT v3 (MANDATORY)
You prepare source; you do not deploy it. Do not call cloud deployment tools or provider CLIs.

Identity and reserved handoff:
- job_id: {contract["job_id"]}
- app_id: {contract["app_id"]}
- canonical app repository: {contract["app_root"]}
- captured branch: {contract["branch"]}
- captured base B: {base}
- worktree_id: {contract["worktree_id"]}
- reserved detached worktree path: {contract["workspace_path"]}

{dirty}

The canonical app must be clean when you finish. You own its local commits. Do not deploy, fetch, push, switch branches, rewrite existing history, stash, reset, or clean.

Choose exactly one completion mode:

MODE `change` — use for a current-state app change (not a historical redeploy):
1. If captured base is the bootstrap placeholder, create one baseline root commit B from the untouched existing app files before making task changes. Otherwise B is the captured base above.
2. Finish the requested and pre-existing app work. Keep README.md and aios.deploy.yaml accurate; store only environment names and secret-reference IDs, never secret values.
3. Append a UTC ISO-8601 timestamped entry to HISTORY.md containing this exact job ID, the rollback base B, a plain-language summary, important files, and verification performed. Include `job_id: {contract["job_id"]}` and `rollback_base: <full-B-oid>` literally so the host can validate the record.
4. Commit all durable app work, including HISTORY.md, leaving a clean source tip C. C may be one or more new linear, non-merge commits descending from B. Do not create a later metadata-only commit. Canonical HEAD must equal C.
5. Resolve C's full commit and tree OIDs and create the reserved workspace with: git worktree add --detach {contract["workspace_path"]} <full-C-oid>

MODE `selected_commit` — use only when the user asked to redeploy a historical state:
1. If unfinished changes existed at start, first incorporate and verify them, append the required HISTORY.md record with this job ID and rollback base B, and commit them as a clean current source tip R. This preserves current work but does not make R the deployment selection. If the app was clean, leave canonical source unchanged at B.
2. Search HISTORY.md, legacy checkpoints, git log, and diffs for the requested historical state.
3. Select the exact full commit C representing that state. If multiple commits plausibly match, stop and report candidates instead of preparing a handoff.
4. Leave the canonical repository clean at B or recovered tip R, then create the reserved detached worktree at selected C.

For either mode, write {contract["workspace_path"]}/.aios/CODEX_HANDOFF.json as valid JSON with exactly these fields:
{{
  "schema_version": 2,
  "job_id": "{contract["job_id"]}",
  "app_id": "{contract["app_id"]}",
  "mode": "change or selected_commit",
  "worktree_id": "{contract["worktree_id"]}",
  "canonical_repository": "{contract["app_root"]}",
  "workspace_path": "{contract["workspace_path"]}",
  "base_commit": "full B OID",
  "source_commit": "full C OID",
  "source_tree": "full C tree OID",
  "selection_reason": "concise evidence for why this source state matches"
}}

Do not put build output, caches, dependencies, or any other uncommitted files in the detached worktree. After writing the descriptor, do not modify or remove that worktree. Report the mode, full B/C OIDs, verification, worktree_id, and path. The host records the machine-readable checkpoint after validating C; you must not create commit M or write a per-job checkpoint JSON.
"""


def _app_record_contract_task(task: str, contract: dict[str, Any]) -> str:
    """Attach the v3 B-to-C app history contract without deployment."""

    base = contract.get("base_commit") or "<bootstrap-baseline-created-by-codex>"
    dirty = _dirty_recovery_instructions(contract)
    return f"""{task.strip()}

AIOS APP CHANGE RECORD CONTRACT v3 (MANDATORY)
This is a durable AIOS app repository. You own its local commits; do not deploy, fetch, push, switch branches, rewrite existing history, stash, reset, or clean it.

- job_id: {contract["job_id"]}
- app_id: {contract["app_id"]}
- canonical app repository: {contract["app_root"]}
- captured branch: {contract["branch"]}
- captured base B: {base}

{dirty}

If this is read-only and the app was clean at start, leave it unchanged and clean. If the repository was just initialized, first create a baseline root commit B containing the untouched app files.

If you make a material change or unfinished changes existed at start:
1. Use the captured B, or create the untouched bootstrap baseline B before editing.
2. Finish the requested and pre-existing work. Keep README.md and aios.deploy.yaml accurate and never store secret values.
3. Append a UTC ISO-8601 timestamped entry to HISTORY.md containing `job_id: {contract["job_id"]}`, `rollback_base: <full-B-oid>`, a summary, important files, and verification performed.
4. Commit all durable work and finish clean at source tip C. C may be one or more new linear, non-merge commits descending from B. Do not create a later metadata-only commit or per-job checkpoint JSON.
5. Report full B/C OIDs and verification. The host records the machine-readable checkpoint after validating C.
"""


def _read_app_id(app_root: Path) -> str:
    metadata_path = app_root / ".aios-app.json"
    if metadata_path.is_file():
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("app_id"), str):
            return payload["app_id"]
    try:
        return load_deployment_manifest(app_root).app_id
    except ManifestValidationError as exc:
        raise AppGitError(
            "Deploy-requested Codex jobs require .aios-app.json or a valid "
            f"aios.deploy.yaml: {exc}"
        ) from exc


def _find_registered_app_root(
    start: Path, registry: WorktreeRegistry
) -> tuple[Path, str] | None:
    for candidate in (start, *start.parents):
        if not (
            (candidate / ".aios-app.json").is_file()
            or (candidate / "aios.deploy.yaml").is_file()
        ):
            continue
        try:
            app_id = _read_app_id(candidate)
        except AppGitError:
            continue
        expected = (registry.apps_root / app_id).resolve()
        if candidate.resolve() == expected:
            return candidate.resolve(), app_id
    return None


def _prepare_app_contract(
    *,
    app_root: Path,
    app_id: str,
    job_id: str,
    deployment_requested: bool,
) -> dict[str, Any]:
    has_git = (app_root / ".git").exists()
    has_head = (
        has_git
        and run_git(app_root, ["rev-parse", "--verify", "HEAD"], check=False).returncode
        == 0
    )
    bootstrap = not has_head
    inventory = _bootstrap_inventory(app_root) if bootstrap else None
    if bootstrap:
        if not has_git:
            run_git(app_root, ["init", "-b", "main"])
        branch_result = run_git(
            app_root,
            ["symbolic-ref", "--quiet", "--short", "HEAD"],
            check=False,
        )
        branch = branch_result.stdout.strip() or "main"
        base_commit = None
        initial_dirty = False
        initial_status = ""
    else:
        # Codex owns app commits. Permit unfinished bytes into the editing
        # phase so interrupted work can self-heal; completion and artifact
        # boundaries still require a clean, validated source commit.
        state = inspect_app_repository(app_root, require_clean=False)
        branch = state.branch
        base_commit = state.commit
        initial_dirty = not state.clean
        initial_status = state.status.rstrip()
    return {
        "contract_version": 3,
        "deployment_requested": deployment_requested,
        "job_id": job_id,
        "app_id": app_id,
        "app_root": str(app_root),
        "canonical_repository": str(app_root),
        "branch": branch,
        "base_commit": base_commit,
        "bootstrap": bootstrap,
        "baseline_inventory_sha256": inventory,
        "initial_dirty": initial_dirty,
        "initial_status": initial_status,
        "followups": 0,
    }


def _bootstrap_inventory(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root)
        if relative.parts and relative.parts[0] == ".git":
            continue
        if path.is_symlink():
            raise AppGitError(f"Bootstrap app contains unsupported symlink: {relative}")
        if not path.is_file():
            continue
        name = relative.as_posix().encode("utf-8")
        data = path.read_bytes()
        digest.update(len(name).to_bytes(4, "big"))
        digest.update(name)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def _commit_inventory(root: Path, commit: str) -> str:
    digest = hashlib.sha256()
    names = run_git(root, ["ls-tree", "-r", "-z", "--name-only", commit]).stdout
    for relative in sorted(name for name in names.split("\0") if name):
        contents = read_blob(root, commit, relative)
        name = relative.encode("utf-8")
        digest.update(len(name).to_bytes(4, "big"))
        digest.update(name)
        digest.update(len(contents).to_bytes(8, "big"))
        digest.update(contents)
    return digest.hexdigest()


def _contains_secret_value(value: Any) -> bool:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = str(key).casefold().replace("-", "_")
            if (
                normalized
                in {
                    "password",
                    "secret",
                    "secret_value",
                    "token",
                    "api_key",
                    "private_key",
                }
                and nested is not None
                and nested != ""
                and nested is not False
            ):
                return True
            if _contains_secret_value(nested):
                return True
    elif isinstance(value, list):
        return any(_contains_secret_value(item) for item in value)
    return False


def _validate_v3_history(
    app_root: Path,
    *,
    source_commit: str,
    base_commit: str,
    job_id: str,
    changed_files: tuple[str, ...],
) -> None:
    """Require the human-readable v3 change record inside source commit C."""

    if "HISTORY.md" not in changed_files:
        raise AppGitError("Source range C must update HISTORY.md")
    try:
        history = read_blob(app_root, source_commit, "HISTORY.md").decode("utf-8")
    except (AppGitError, UnicodeDecodeError) as exc:
        raise AppGitError("Could not read UTF-8 HISTORY.md from source commit C") from exc
    if job_id not in history:
        raise AppGitError("HISTORY.md must contain this Codex job ID")
    if base_commit not in history:
        raise AppGitError("HISTORY.md must contain the full rollback base B")


def _host_checkpoint(
    *,
    job_id: str,
    app_id: str,
    mode: str,
    base_commit: str,
    source_commit: str,
    source_tree: str,
    changed_files: tuple[str, ...] | list[str],
    selection_reason: str,
) -> dict[str, Any]:
    """Build the host-owned v3 checkpoint persisted in ``CodexRunStore``."""

    return {
        "schema_version": 2,
        "job_id": job_id,
        "app_id": app_id,
        "mode": mode,
        "base_commit": base_commit,
        "source_commit": source_commit,
        "source_tree": source_tree,
        "changed_files": list(changed_files),
        "selection_reason": selection_reason,
        "verification_evidence": "HISTORY.md in source commit C",
    }


def _mcp_result_payload(result: Any) -> dict[str, Any] | None:
    """Extract a JSON object from an app-server MCP result envelope."""

    if not isinstance(result, dict):
        return None
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        nested = structured.get("result")
        return nested if isinstance(nested, dict) else structured
    content = result.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            try:
                payload = json.loads(str(block.get("text") or ""))
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                return payload
    # Keeping direct dictionaries supported makes the protocol adapter tolerant
    # of lightweight MCP clients and deterministic test servers.
    return result


def _process_identity(pid: int) -> str | None:
    """Return a PID-reuse-safe identity for a locally running process."""

    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value or None


def _normalize_app_server_item(item: dict[str, Any]) -> dict[str, Any]:
    """Convert app-server camelCase items to the exec JSONL shape."""
    normalized = dict(item)
    normalized["type"] = {
        "agentMessage": "agent_message",
        "commandExecution": "command_execution",
        "fileChange": "file_change",
        "mcpToolCall": "mcp_tool_call",
        "dynamicToolCall": "dynamic_tool_call",
        "webSearch": "web_search",
    }.get(str(item.get("type")), item.get("type"))
    for source, target in (
        ("aggregatedOutput", "aggregated_output"),
        ("exitCode", "exit_code"),
    ):
        if source in item:
            normalized[target] = item[source]
    return normalized


class _PendingRpc:
    def __init__(self) -> None:
        self.ready = threading.Event()
        self.response: dict[str, Any] | None = None


class CodexJob:
    """One isolated app-server process and one Codex thread/turn."""

    def __init__(
        self,
        job_id: str,
        task: str,
        workdir: str,
        cmd: list[str],
        *,
        model: str | None = None,
        session_id: str | None = None,
        parent_tool_call_id: str | None = None,
        store: CodexRunStore,
        enable_deploy: bool = False,
        deploy_state: dict[str, Any] | None = None,
        app_contract: dict[str, Any] | None = None,
        worktree_registry: WorktreeRegistry | None = None,
        unexpected_exit_sink: _UnexpectedExitSink | None = None,
        resume_thread_id: str | None = None,
        recovery_prompt: str | None = None,
    ) -> None:
        self.id = job_id
        self.task = task
        self.workdir = workdir
        self.cmd = cmd
        self.model = model
        self.session_id = session_id
        self.parent_tool_call_id = parent_tool_call_id
        self.store = store
        self.enable_deploy = enable_deploy
        self.app_contract = dict(app_contract) if app_contract else None
        self.worktree_registry = worktree_registry
        self._unexpected_exit_sink = unexpected_exit_sink
        restored_handoff = (self.app_contract or {}).get("workspace_handoff")
        self.workspace_handoff = (
            dict(restored_handoff) if isinstance(restored_handoff, dict) else None
        )
        self.resume_thread_id = resume_thread_id
        self.recovery_prompt = recovery_prompt
        self.status = "running"
        self.error: str | None = None
        self.result: str | None = None
        self.events: list[dict[str, Any]] = []
        self.started_at = monotonic()
        self.finished_at: float | None = None
        self.thread_id: str | None = None
        self.turn_id: str | None = None
        self._final_message: str | None = None
        self._pending_input: dict[str, Any] | None = None
        self._pending_input_request_id: int | str | None = None
        self._proc: subprocess.Popen[str] | None = None
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._new = threading.Event()
        self._next_request_id = 1
        self._pending_rpc: dict[int, _PendingRpc] = {}
        self._stderr_chunks: list[str] = []
        self._finishing = False
        self._recovery_requested = False
        restored_deploy_state = deploy_state or {}
        self._deploy_tools_called = {
            str(tool) for tool in restored_deploy_state.get("called", [])
        }
        self._deploy_tools_enqueued = {
            str(tool) for tool in restored_deploy_state.get("enqueued", [])
        }
        restored_results = restored_deploy_state.get("last_results")
        self._deploy_last_results = (
            dict(restored_results) if isinstance(restored_results, dict) else {}
        )
        self._deploy_followups = int(restored_deploy_state.get("followups") or 0)
        self._app_handoff_followups = int(
            (self.app_contract or {}).get("followups") or 0
        )

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if not self.session_id:
            return
        enriched = {
            "job_id": self.id,
            "parent_tool_call_id": self.parent_tool_call_id,
            **payload,
        }
        try:
            sequence = self.store.append_gateway_event(
                self.id, self.session_id, event_type, enriched
            )
        except Exception:
            log.exception("Failed to persist Codex gateway event %s", event_type)
            return
        if _progress_sink is None:
            return
        enriched["codex_event_id"] = f"{self.id}:{sequence}"
        try:
            _progress_sink(self.session_id, event_type, enriched)
            self.store.complete_gateway_event(self.id, sequence)
        except Exception:
            log.exception("Failed to publish Codex gateway event %s", event_type)

    def _emit_lifecycle(self, status: str) -> None:
        if _lifecycle_sink is None or not self.session_id:
            return
        try:
            _lifecycle_sink(self.session_id, self.id, status)
        except Exception:
            pass

    def _append_event(self, event: dict[str, Any]) -> None:
        bounded = dict(event)
        for key in ("input", "output", "error"):
            value = bounded.get(key)
            if isinstance(value, str) and len(value) > MAX_EVENT_TEXT_CHARS:
                bounded[key] = (
                    value[:MAX_EVENT_TEXT_CHARS]
                    + "\n... (truncated by Codex event limit)"
                )
        with self._lock:
            self.events.append(bounded)
        self.store.append_event(self.id, bounded)
        self._new.set()

    def _append_stderr(self, text: str) -> None:
        with self._lock:
            self._stderr_chunks.append(text)
            total = sum(len(chunk) for chunk in self._stderr_chunks)
            while self._stderr_chunks and total > MAX_STDERR_CHARS:
                total -= len(self._stderr_chunks.pop(0))

    def start(self) -> None:
        self._proc = subprocess.Popen(
            self.cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=self.workdir,
            start_new_session=True,
        )
        pid = getattr(self._proc, "pid", None)
        if isinstance(pid, int):
            self.store.update(
                self.id,
                process_pid=pid,
                process_identity=_process_identity(pid),
            )
        log.info(
            "Codex job started",
            extra={
                "codex_job_id": self.id,
                "codex_session_id": self.session_id,
                "codex_recovered": bool(self.resume_thread_id),
            },
        )
        self._emit("codex.started", {"task_summary": self.task[:200]})
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        threading.Thread(target=self._bootstrap, daemon=True).start()
        threading.Thread(target=self._watchdog, daemon=True).start()

    def _send(self, message: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.poll() is not None:
            raise RuntimeError("Codex app-server is not running")
        encoded = json.dumps(message, separators=(",", ":")) + "\n"
        with self._write_lock:
            proc.stdin.write(encoded)
            proc.stdin.flush()

    def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            request_id = self._next_request_id
            self._next_request_id += 1
            pending = _PendingRpc()
            self._pending_rpc[request_id] = pending
        try:
            self._send(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            )
            if not pending.ready.wait(RPC_TIMEOUT_SECONDS):
                raise TimeoutError(
                    "Codex app-server did not answer "
                    f"{method} within {RPC_TIMEOUT_SECONDS:g}s"
                )
            response = pending.response or {}
            if response.get("error") is not None:
                raise RuntimeError(f"{method} failed: {response['error']}")
            result = response.get("result")
            return result if isinstance(result, dict) else {}
        finally:
            with self._lock:
                self._pending_rpc.pop(request_id, None)

    def _bootstrap(self) -> None:
        try:
            self._rpc(
                "initialize",
                {
                    "clientInfo": {"name": "mini-aios", "version": "1"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            self._send({"jsonrpc": "2.0", "method": "initialized"})
            thread_params: dict[str, Any] = {
                "cwd": self.workdir,
                "runtimeWorkspaceRoots": [self.workdir],
                "approvalPolicy": "never",
                "sandbox": "danger-full-access",
                "ephemeral": False,
            }
            if self.model:
                thread_params["model"] = self.model
            method = "thread/start"
            if self.resume_thread_id:
                method = "thread/resume"
                thread_params["threadId"] = self.resume_thread_id
            thread_result = self._rpc(method, thread_params)
            thread = thread_result.get("thread") or {}
            self.thread_id = (
                str(thread.get("id") or self.resume_thread_id or "") or None
            )
            if not self.thread_id:
                raise RuntimeError("thread/start returned no thread id")
            turn_result = self._rpc(
                "turn/start",
                {
                    "threadId": self.thread_id,
                    "input": [
                        {
                            "type": "text",
                            "text": self.recovery_prompt or self.task,
                            "text_elements": [],
                        }
                    ],
                },
            )
            turn = turn_result.get("turn") or {}
            self.turn_id = str(turn.get("id") or "") or None
            self.store.update(self.id, thread_id=self.thread_id, turn_id=self.turn_id)
            self._new.set()
        except Exception as exc:
            proc = self._proc
            if proc is not None and proc.poll() is not None:
                self._request_process_recovery(str(exc))
            else:
                self._finish("error", error=str(exc))

    def _read_stdout(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        try:
            for line in iter(proc.stdout.readline, ""):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    message = json.loads(stripped)
                except json.JSONDecodeError:
                    self._append_stderr(stripped)
                    continue
                self._handle_message(message)
        except Exception as exc:
            if self.status not in _TERMINAL_STATUSES:
                self._request_process_recovery(
                    f"Codex protocol reader failed: {exc}"
                )
        finally:
            returncode = proc.wait()
            if self.status not in _TERMINAL_STATUSES:
                detail = "".join(self._stderr_chunks).strip()
                self._request_process_recovery(
                    detail or f"Codex app-server exited {returncode}"
                )

    def _request_process_recovery(self, detail: str) -> None:
        """Report one unexpected child failure without terminalizing the run."""

        with self._lock:
            if (
                self.status in _TERMINAL_STATUSES
                or self._finishing
                or self._recovery_requested
            ):
                return
            self._recovery_requested = True
            self._finishing = True
            pending_calls = list(self._pending_rpc.values())
        self._terminate_live_process()
        for pending in pending_calls:
            pending.ready.set()
        self._new.set()
        if self._unexpected_exit_sink is None:
            with self._lock:
                self._finishing = False
            self._finish("error", error=detail)
            return
        self._unexpected_exit_sink(self, detail)

    def _read_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for line in iter(proc.stderr.readline, ""):
            if line:
                self._append_stderr(line)

    def _handle_message(self, message: Any) -> None:
        if not isinstance(message, dict):
            return
        response_id = message.get("id")
        if response_id is not None and "method" not in message:
            with self._lock:
                pending = self._pending_rpc.get(response_id)
                if pending is not None:
                    pending.response = message
                    pending.ready.set()
            return

        method = message.get("method")
        params = (
            message.get("params") if isinstance(message.get("params"), dict) else {}
        )
        if method == "item/tool/requestUserInput" and response_id is not None:
            self._request_user_input(response_id, params)
        elif method in {"item/started", "item/completed"}:
            self._handle_item(method, params)
        elif method == "turn/started":
            turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
            self.turn_id = str(turn.get("id") or self.turn_id or "") or None
            if self.turn_id:
                self.store.update(self.id, turn_id=self.turn_id)
        elif method == "turn/completed":
            self._handle_turn_completed(params)

    def _handle_item(self, method: str, params: dict[str, Any]) -> None:
        item = params.get("item")
        if not isinstance(item, dict):
            return
        if method == "item/completed" and item.get("type") == "mcpToolCall":
            server = str(item.get("server") or "")
            tool = str(item.get("tool") or item.get("name") or "")
            if server == "deploy" and tool in _DEPLOY_TOOL_BY_COMPONENT.values():
                with self._lock:
                    self._deploy_tools_called.add(tool)
                    payload = _mcp_result_payload(item.get("result"))
                    self._deploy_last_results[tool] = (
                        payload
                        or item.get("error")
                        or {"status": str(item.get("status") or "unknown")}
                    )
                    deployment_id = payload.get("id") if payload else None
                    if isinstance(deployment_id, str) and deployment_id.startswith(
                        "dep_"
                    ):
                        self._deploy_tools_enqueued.add(tool)
                    self._persist_deploy_state()
        event_type = "item.started" if method.endswith("started") else "item.completed"
        for desc in translate_codex_event(
            {"type": event_type, "item": _normalize_app_server_item(item)}
        ):
            if desc["kind"] == "text":
                self._final_message = desc["value"]
                self._emit(
                    "codex.progress", {"kind": "message", "detail": desc["value"][:500]}
                )
                continue
            self._append_event(desc)
            tool = desc.get("tool_name", "tool")
            kind = (
                "command"
                if tool == "command_execution"
                else "file"
                if tool == "file_change"
                else tool
            )
            self._emit(
                "codex.progress",
                {
                    "kind": kind,
                    "phase": desc["kind"],
                    "tool_call_id": desc.get("tool_call_id"),
                    "detail": str(desc.get("input") or desc.get("output") or "")[:500],
                },
            )

    def _request_user_input(
        self, request_id: int | str, params: dict[str, Any]
    ) -> None:
        questions = (
            params.get("questions") if isinstance(params.get("questions"), list) else []
        )
        pending_input = {
            "item_id": params.get("itemId"),
            "thread_id": params.get("threadId"),
            "turn_id": params.get("turnId"),
            "is_blocking": bool(params.get("isBlocking", True)),
            "questions": questions,
        }
        with self._lock:
            if self._pending_input_request_id is not None:
                self._send_error_response(
                    request_id, -32000, "another input request is already pending"
                )
                return
            self._pending_input_request_id = request_id
            self._pending_input = pending_input
            self.status = "awaiting_input"
        self._append_event({"kind": "input_requested", "input": pending_input})
        self.store.update(self.id, status="awaiting_input", pending_input=pending_input)
        self.store.enqueue_signal(self.id, "awaiting_input")
        log.info(
            "Codex job awaiting input",
            extra={"codex_job_id": self.id, "codex_session_id": self.session_id},
        )
        self._emit("codex.input.requested", pending_input)
        self._emit_lifecycle("awaiting_input")

    def _send_error_response(
        self, request_id: int | str, code: int, message: str
    ) -> None:
        try:
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": code, "message": message},
                }
            )
        except Exception:
            pass

    @staticmethod
    def _normalize_answers(answers: dict[str, Any]) -> dict[str, dict[str, list[str]]]:
        normalized: dict[str, dict[str, list[str]]] = {}
        for question_id, value in answers.items():
            if isinstance(value, dict):
                value = value.get("answers", [])
            if isinstance(value, str):
                values = [value]
            elif isinstance(value, list):
                values = [str(item) for item in value]
            else:
                values = [str(value)]
            normalized[str(question_id)] = {"answers": values}
        return normalized

    def answer(self, answers: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(answers, dict) or not answers:
            return {"error": "answers must be a non-empty object"}
        with self._lock:
            request_id = self._pending_input_request_id
            pending_input = self._pending_input
        if request_id is None or pending_input is None:
            return {"error": f"job {self.id} is not awaiting input"}
        normalized = self._normalize_answers(answers)
        expected = {
            str(q.get("id")) for q in pending_input.get("questions", []) if q.get("id")
        }
        missing = sorted(expected - normalized.keys())
        if missing:
            return {"error": f"missing answers for: {', '.join(missing)}"}
        try:
            self._send(
                {"jsonrpc": "2.0", "id": request_id, "result": {"answers": normalized}}
            )
        except Exception as exc:
            return {"error": f"failed to answer Codex: {exc}"}
        with self._lock:
            self._pending_input_request_id = None
            self._pending_input = None
            if self.status == "awaiting_input":
                self.status = "running"
        self.store.update(self.id, status="running", clear_pending_input=True)
        self._new.set()
        self._emit("codex.input.resolved", {"question_ids": sorted(normalized)})
        return {"job_id": self.id, "status": self.status}

    def _handle_turn_completed(self, params: dict[str, Any]) -> None:
        turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
        for item in turn.get("items") or []:
            if (
                isinstance(item, dict)
                and item.get("type") == "agentMessage"
                and item.get("text")
            ):
                self._final_message = str(item["text"])
        turn_status = turn.get("status")
        if turn_status == "completed":
            app_issue = self._app_handoff_guard_issue()
            if app_issue is not None:
                if self._app_handoff_followups < MAX_APP_HANDOFF_FOLLOWUPS:
                    self._app_handoff_followups += 1
                    if self.app_contract is not None:
                        self.app_contract["followups"] = self._app_handoff_followups
                        self.store.update(self.id, app_state=self.app_contract)
                    self._append_event(
                        {
                            "kind": "app_handoff_guard",
                            "output": app_issue,
                            "attempt": self._app_handoff_followups,
                        }
                    )
                    threading.Thread(
                        target=self._start_app_handoff_followup,
                        args=(app_issue,),
                        daemon=True,
                    ).start()
                    return
                self._finish(
                    "error",
                    error=(
                        "Codex did not satisfy the mandatory AIOS app handoff "
                        f"contract after {MAX_APP_HANDOFF_FOLLOWUPS} follow-up turns: "
                        f"{app_issue}"
                    ),
                )
                return
            issue = self._deployment_guard_issue()
            if issue is not None:
                if self._deploy_followups < MAX_DEPLOY_FOLLOWUPS:
                    self._deploy_followups += 1
                    self._persist_deploy_state()
                    self._append_event(
                        {
                            "kind": "deployment_guard",
                            "output": issue,
                            "attempt": self._deploy_followups,
                        }
                    )
                    self._emit(
                        "codex.progress",
                        {
                            "kind": "deployment_guard",
                            "phase": "continuing",
                            "detail": issue[:500],
                        },
                    )
                    threading.Thread(
                        target=self._start_deploy_followup,
                        args=(issue,),
                        daemon=True,
                    ).start()
                    return
                self._finish(
                    "error",
                    error=(
                        "Codex did not satisfy the mandatory AIOS deployment "
                        f"contract after {MAX_DEPLOY_FOLLOWUPS} follow-up turns: {issue}"
                    ),
                )
                return
            self._finish("done", result=self._final_message or "(empty)")
        elif turn_status == "interrupted":
            self._finish("cancelled", error="Codex turn was interrupted")
        else:
            turn_error = (
                turn.get("error") if isinstance(turn.get("error"), dict) else {}
            )
            self._finish(
                "error", error=str(turn_error.get("message") or "Codex turn failed")
            )

    def _app_handoff_guard_issue(self) -> str | None:
        contract = self.app_contract
        if contract is None:
            return None
        if int(contract.get("contract_version") or 2) < 3:
            return self._app_handoff_guard_issue_v2()
        if not contract.get("deployment_requested"):
            return self._app_record_guard_issue_v3(contract)
        return self._app_workspace_guard_issue_v3(contract)

    def _app_handoff_guard_issue_v2(self) -> str | None:
        contract = self.app_contract
        if contract is None:
            return None
        if not contract.get("deployment_requested"):
            return self._app_record_guard_issue_v2(contract)
        registry = self.worktree_registry
        if registry is None:
            return "The host worktree registry is unavailable."
        descriptor_path = (
            Path(str(contract["workspace_path"])) / ".aios" / "CODEX_HANDOFF.json"
        )
        try:
            if descriptor_path.is_symlink():
                raise WorktreeHandoffError("CODEX_HANDOFF.json must not be a symlink")
            payload = json.loads(descriptor_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise WorktreeHandoffError("CODEX_HANDOFF.json must contain an object")
            required = {
                "schema_version",
                "job_id",
                "app_id",
                "mode",
                "worktree_id",
                "canonical_repository",
                "workspace_path",
                "base_commit",
                "source_commit",
                "source_tree",
                "provenance_commit",
                "selection_reason",
            }
            if set(payload) != required:
                missing = sorted(required - set(payload))
                extra = sorted(set(payload) - required)
                raise WorktreeHandoffError(
                    f"CODEX_HANDOFF.json fields mismatch; missing={missing}, extra={extra}"
                )
            for key in (
                "job_id",
                "app_id",
                "worktree_id",
                "canonical_repository",
                "workspace_path",
            ):
                if str(payload.get(key)) != str(contract.get(key)):
                    raise WorktreeHandoffError(
                        f"Handoff {key} does not match reservation"
                    )
            if payload.get("schema_version") != 1:
                raise WorktreeHandoffError("Unsupported CODEX_HANDOFF schema version")
            mode = payload.get("mode")
            if mode not in {"change", "selected_commit"}:
                raise WorktreeHandoffError(
                    "Handoff mode must be change or selected_commit"
                )

            app_root = Path(str(contract["app_root"])).resolve()
            canonical = inspect_app_repository(app_root, require_clean=True)
            if canonical.branch != contract.get("branch"):
                raise WorktreeHandoffError("Codex changed the canonical app branch")
            source = resolve_commit(app_root, str(payload["source_commit"]))
            source_tree = resolve_tree(app_root, source)
            if source_tree != str(payload["source_tree"]):
                raise WorktreeHandoffError("Handoff source_tree is not C's Git tree")

            provenance: str | None = None
            if mode == "change":
                base = resolve_commit(app_root, str(payload["base_commit"]))
                captured_base = contract.get("base_commit")
                if captured_base and base != captured_base:
                    raise WorktreeHandoffError(
                        "Change commit is not based on captured B"
                    )
                if not captured_base:
                    expected_inventory = contract.get("baseline_inventory_sha256")
                    if (
                        not expected_inventory
                        or _commit_inventory(app_root, base) != expected_inventory
                    ):
                        raise WorktreeHandoffError(
                            "Codex bootstrap baseline B does not match the untouched app tree"
                        )
                provenance_value = payload.get("provenance_commit")
                if not isinstance(provenance_value, str):
                    raise WorktreeHandoffError(
                        "Change handoff requires provenance commit M"
                    )
                topology = validate_change_topology(
                    app_root,
                    base_commit=base,
                    change_commit=source,
                    provenance_commit=provenance_value,
                    job_id=self.id,
                )
                provenance = topology.provenance_commit
                if canonical.commit != provenance:
                    raise WorktreeHandoffError("Canonical app HEAD must equal M")
                checkpoint_path = app_root / ".aios" / "checkpoints" / f"{self.id}.json"
                checkpoint = load_app_checkpoint(checkpoint_path)
                validate_app_checkpoint(
                    checkpoint,
                    repository=app_root,
                    job_id=self.id,
                    app_id=str(contract["app_id"]),
                    base_commit=topology.base_commit,
                    change_commit=topology.change_commit,
                )
                if _contains_secret_value(checkpoint.model_dump(mode="json")):
                    raise WorktreeHandoffError(
                        "Checkpoint contains a secret-like value field"
                    )
            else:
                captured_base = contract.get("base_commit")
                if not captured_base:
                    raise WorktreeHandoffError(
                        "Historical selection requires an existing captured repository"
                    )
                if canonical.commit != captured_base:
                    raise WorktreeHandoffError(
                        "Historical selection changed the canonical app repository"
                    )
                if payload.get("provenance_commit") is not None:
                    provenance = resolve_commit(
                        app_root, str(payload["provenance_commit"])
                    )

            selection_reason = payload.get("selection_reason")
            if not isinstance(selection_reason, str) or not selection_reason.strip():
                raise WorktreeHandoffError("Handoff selection_reason is required")
            published = registry.publish_handoff(
                str(contract["worktree_id"]),
                source_commit=source,
                source_tree=source_tree,
                provenance_commit=provenance,
                selection_reason=selection_reason.strip(),
            )
            self.workspace_handoff = {
                "handoff_id": published.handoff_id,
                "worktree_id": published.worktree_id,
                "app_id": published.app_id,
                "canonical_repository": published.repository,
                "workspace_path": published.path,
                "source_commit": published.source_commit,
                "source_tree": published.source_tree,
                "provenance_commit": published.provenance_commit,
                "selection_reason": published.selection_reason,
                "mode": mode,
                "status": published.status,
            }
            contract["workspace_handoff"] = self.workspace_handoff
            self.store.update(self.id, app_state=contract)
            return None
        except (
            AppGitError,
            AppCheckpointError,
            WorktreeHandoffError,
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            return f"Workspace handoff validation failed: {exc}"

    def _app_workspace_guard_issue_v3(
        self, contract: dict[str, Any]
    ) -> str | None:
        """Validate a v3 B-to-C change or historical detached handoff."""

        registry = self.worktree_registry
        if registry is None:
            return "The host worktree registry is unavailable."
        descriptor_path = (
            Path(str(contract["workspace_path"])) / ".aios" / "CODEX_HANDOFF.json"
        )
        try:
            if descriptor_path.is_symlink():
                raise WorktreeHandoffError("CODEX_HANDOFF.json must not be a symlink")
            payload = json.loads(descriptor_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise WorktreeHandoffError("CODEX_HANDOFF.json must contain an object")
            required = {
                "schema_version",
                "job_id",
                "app_id",
                "mode",
                "worktree_id",
                "canonical_repository",
                "workspace_path",
                "base_commit",
                "source_commit",
                "source_tree",
                "selection_reason",
            }
            if set(payload) != required:
                missing = sorted(required - set(payload))
                extra = sorted(set(payload) - required)
                raise WorktreeHandoffError(
                    f"CODEX_HANDOFF.json fields mismatch; missing={missing}, extra={extra}"
                )
            for key in (
                "job_id",
                "app_id",
                "worktree_id",
                "canonical_repository",
                "workspace_path",
            ):
                if str(payload.get(key)) != str(contract.get(key)):
                    raise WorktreeHandoffError(
                        f"Handoff {key} does not match reservation"
                    )
            if payload.get("schema_version") != 2:
                raise WorktreeHandoffError("Unsupported CODEX_HANDOFF schema version")
            mode = payload.get("mode")
            if mode not in {"change", "selected_commit"}:
                raise WorktreeHandoffError(
                    "Handoff mode must be change or selected_commit"
                )

            app_root = Path(str(contract["app_root"])).resolve()
            canonical = inspect_app_repository(app_root, require_clean=True)
            if canonical.branch != contract.get("branch"):
                raise WorktreeHandoffError("Codex changed the canonical app branch")
            source = resolve_commit(app_root, str(payload["source_commit"]))
            source_tree = resolve_tree(app_root, source)
            if source_tree != str(payload["source_tree"]):
                raise WorktreeHandoffError("Handoff source_tree is not C's Git tree")

            base = resolve_commit(app_root, str(payload["base_commit"]))
            captured_base = contract.get("base_commit")
            changed_files: tuple[str, ...] = ()
            if mode == "change":
                if captured_base and base != captured_base:
                    raise WorktreeHandoffError(
                        "Change source is not based on captured B"
                    )
                if not captured_base:
                    expected_inventory = contract.get("baseline_inventory_sha256")
                    if (
                        not expected_inventory
                        or _commit_inventory(app_root, base) != expected_inventory
                    ):
                        raise WorktreeHandoffError(
                            "Codex bootstrap baseline B does not match the untouched app tree"
                        )
                source_range = validate_source_range(
                    app_root,
                    base_commit=base,
                    source_commit=source,
                )
                changed_files = source_range.changed_files
                if canonical.commit != source:
                    raise WorktreeHandoffError("Canonical app HEAD must equal source C")
                _validate_v3_history(
                    app_root,
                    source_commit=source,
                    base_commit=base,
                    job_id=self.id,
                    changed_files=changed_files,
                )
            else:
                if not captured_base:
                    raise WorktreeHandoffError(
                        "Historical selection requires an existing captured repository"
                    )
                if base != captured_base:
                    raise WorktreeHandoffError(
                        "Historical selection base does not match captured B"
                    )
                if contract.get("initial_dirty"):
                    recovered = validate_source_range(
                        app_root,
                        base_commit=base,
                        source_commit=canonical.commit,
                    )
                    _validate_v3_history(
                        app_root,
                        source_commit=canonical.commit,
                        base_commit=base,
                        job_id=self.id,
                        changed_files=recovered.changed_files,
                    )
                    contract["recovered_current_checkpoint"] = _host_checkpoint(
                        job_id=self.id,
                        app_id=str(contract["app_id"]),
                        mode="recovered_current",
                        base_commit=base,
                        source_commit=canonical.commit,
                        source_tree=canonical.tree,
                        changed_files=recovered.changed_files,
                        selection_reason=(
                            "Preserved unfinished current app work before historical selection"
                        ),
                    )
                elif canonical.commit != captured_base:
                    raise WorktreeHandoffError(
                        "Historical selection changed the canonical app repository"
                    )

            selection_reason = payload.get("selection_reason")
            if not isinstance(selection_reason, str) or not selection_reason.strip():
                raise WorktreeHandoffError("Handoff selection_reason is required")
            reason = selection_reason.strip()
            checkpoint = _host_checkpoint(
                job_id=self.id,
                app_id=str(contract["app_id"]),
                mode=str(mode),
                base_commit=base,
                source_commit=source,
                source_tree=source_tree,
                changed_files=changed_files,
                selection_reason=reason,
            )
            if _contains_secret_value(checkpoint):
                raise WorktreeHandoffError("Host checkpoint contains a secret-like value")

            published = registry.publish_handoff(
                str(contract["worktree_id"]),
                source_commit=source,
                source_tree=source_tree,
                provenance_commit=None,
                selection_reason=reason,
            )
            self.workspace_handoff = {
                "handoff_id": published.handoff_id,
                "worktree_id": published.worktree_id,
                "app_id": published.app_id,
                "canonical_repository": published.repository,
                "workspace_path": published.path,
                "source_commit": published.source_commit,
                "source_tree": published.source_tree,
                "provenance_commit": None,
                "selection_reason": published.selection_reason,
                "mode": mode,
                "status": published.status,
                "contract_version": 3,
            }
            contract.update(
                {
                    "base_commit": base,
                    "change_commit": source,
                    "source_commit": source,
                    "source_tree": source_tree,
                    "provenance_commit": None,
                    "completion_mode": mode,
                    "host_checkpoint": checkpoint,
                    "workspace_handoff": self.workspace_handoff,
                }
            )
            self.store.update(self.id, app_state=contract)
            return None
        except (
            AppGitError,
            WorktreeHandoffError,
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            return f"Workspace handoff validation failed: {exc}"

    def _app_record_guard_issue_v3(self, contract: dict[str, Any]) -> str | None:
        """Validate a clean v3 read-only result or host-recorded B-to-C range."""

        try:
            app_root = Path(str(contract["app_root"])).resolve()
            canonical = inspect_app_repository(app_root, require_clean=True)
            if canonical.branch != contract.get("branch"):
                raise AppGitError("Codex changed the canonical app branch")
            captured_base = contract.get("base_commit")
            if captured_base and canonical.commit == captured_base:
                if contract.get("initial_dirty"):
                    raise AppGitError(
                        "Codex discarded unfinished app changes instead of committing them"
                    )
                contract["completion_mode"] = "read_only"
                self.store.update(self.id, app_state=contract)
                return None

            expected_inventory = contract.get("baseline_inventory_sha256")
            if captured_base:
                base = resolve_commit(app_root, str(captured_base))
            else:
                roots = run_git(
                    app_root,
                    ["rev-list", "--max-parents=0", canonical.commit],
                ).stdout.splitlines()
                if len(roots) != 1:
                    raise AppGitError("Bootstrap app must have exactly one root commit B")
                base = resolve_commit(app_root, roots[0])
                if (
                    not expected_inventory
                    or _commit_inventory(app_root, base) != expected_inventory
                ):
                    raise AppGitError(
                        "Codex bootstrap baseline B does not match the untouched app tree"
                    )
                if canonical.commit == base:
                    contract.update(
                        {"base_commit": base, "completion_mode": "read_only"}
                    )
                    self.store.update(self.id, app_state=contract)
                    return None

            source_range = validate_source_range(
                app_root,
                base_commit=base,
                source_commit=canonical.commit,
            )
            _validate_v3_history(
                app_root,
                source_commit=canonical.commit,
                base_commit=base,
                job_id=self.id,
                changed_files=source_range.changed_files,
            )
            checkpoint = _host_checkpoint(
                job_id=self.id,
                app_id=str(contract["app_id"]),
                mode="change",
                base_commit=base,
                source_commit=canonical.commit,
                source_tree=canonical.tree,
                changed_files=source_range.changed_files,
                selection_reason="Codex completed the requested app change",
            )
            contract.update(
                {
                    "base_commit": base,
                    "change_commit": canonical.commit,
                    "source_commit": canonical.commit,
                    "source_tree": canonical.tree,
                    "provenance_commit": None,
                    "completion_mode": "change",
                    "host_checkpoint": checkpoint,
                }
            )
            self.store.update(self.id, app_state=contract)
            return None
        except (
            AppGitError,
            OSError,
            UnicodeError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            return f"App change record validation failed: {exc}"

    def _app_record_guard_issue_v2(self, contract: dict[str, Any]) -> str | None:
        """Validate a read-only app task or the exact B -> C -> M change record."""

        try:
            app_root = Path(str(contract["app_root"])).resolve()
            canonical = inspect_app_repository(app_root, require_clean=True)
            if canonical.branch != contract.get("branch"):
                raise AppGitError("Codex changed the canonical app branch")
            captured_base = contract.get("base_commit")
            if captured_base and canonical.commit == captured_base:
                contract["completion_mode"] = "read_only"
                self.store.update(self.id, app_state=contract)
                return None

            expected_inventory = contract.get("baseline_inventory_sha256")
            if not captured_base:
                parents = run_git(
                    app_root,
                    ["rev-list", "--parents", "-n", "1", canonical.commit],
                ).stdout.split()
                if (
                    len(parents) == 1
                    and expected_inventory
                    and _commit_inventory(app_root, canonical.commit)
                    == expected_inventory
                ):
                    contract["base_commit"] = canonical.commit
                    contract["completion_mode"] = "read_only"
                    self.store.update(self.id, app_state=contract)
                    return None

            checkpoint_path = app_root / ".aios" / "checkpoints" / f"{self.id}.json"
            checkpoint = load_app_checkpoint(checkpoint_path)
            if _contains_secret_value(checkpoint.model_dump(mode="json")):
                raise AppGitError("Checkpoint contains a secret-like value field")
            base = resolve_commit(app_root, checkpoint.base_commit)
            if captured_base and base != captured_base:
                raise AppGitError("Checkpoint base does not equal captured B")
            if not captured_base:
                if (
                    not expected_inventory
                    or _commit_inventory(app_root, base) != expected_inventory
                ):
                    raise AppGitError(
                        "Codex bootstrap baseline B does not match the untouched app tree"
                    )
            topology = validate_change_topology(
                app_root,
                base_commit=base,
                change_commit=checkpoint.change_commit,
                provenance_commit=canonical.commit,
                job_id=self.id,
            )
            validate_app_checkpoint(
                checkpoint,
                repository=app_root,
                job_id=self.id,
                app_id=str(contract["app_id"]),
                base_commit=topology.base_commit,
                change_commit=topology.change_commit,
            )
            contract.update(
                {
                    "base_commit": topology.base_commit,
                    "change_commit": topology.change_commit,
                    "provenance_commit": topology.provenance_commit,
                    "completion_mode": "change",
                    "checkpoint_path": str(checkpoint_path),
                }
            )
            self.store.update(self.id, app_state=contract)
            return None
        except (
            AppGitError,
            AppCheckpointError,
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            return f"App change record validation failed: {exc}"

    def _start_app_handoff_followup(self, issue: str) -> None:
        try:
            thread_id = self.thread_id
            if not thread_id:
                raise RuntimeError("Codex thread id is unavailable")
            contract = self.app_contract
            if contract is None:
                raise RuntimeError("Codex app contract is unavailable")
            with self._lock:
                self._final_message = None
            if int(contract.get("contract_version") or 2) >= 3:
                repair_text = (
                    "Continue from the current app state and satisfy contract v3. "
                    "Finish all durable app changes, update HISTORY.md with this job ID "
                    "and the full rollback base B, verify the result, and leave clean "
                    "canonical HEAD at source C. A linear non-merge B-to-C range is "
                    "allowed. Do not create metadata commit M, a per-job checkpoint "
                    "file, or rewrite/reset/clean existing history. Recreate the detached "
                    "workspace descriptor with schema_version 2 when deployment was "
                    "requested."
                )
            else:
                checkpoint_contract = _checkpoint_contract_block(contract)
                repair_text = (
                    "CURRENT-JOB METADATA REPAIR AUTHORIZATION:\n"
                    "If the rejection concerns the checkpoint or provenance metadata M, "
                    "inspect every commit after the substantive change commit C. Only "
                    "when every such commit was created by this job and changes no paths "
                    "except HISTORY.md and "
                    f".aios/checkpoints/{self.id}.json, you are explicitly authorized "
                    "to replace that invalid, unpushed metadata suffix: reset the "
                    "canonical branch to C, recreate the exact checkpoint, run the host "
                    "preflight successfully, and create one corrected metadata commit M "
                    "whose sole parent is C. Never rewrite or modify B, C, commits "
                    "predating this job, pushed history, or any suffix containing another "
                    "path. If those safety conditions are not provable, stop and report "
                    "the blocker instead of rewriting history.\n"
                    f"{checkpoint_contract}"
                )
            result = self._rpc(
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": [
                        {
                            "type": "text",
                            "text": (
                                "The host rejected completion because the mandatory "
                                f"AIOS commit/workspace handoff is invalid. {issue} "
                                "Continue in the same canonical app repository and satisfy "
                                "the exact contract. Do not deploy or merely describe steps.\n\n"
                                f"{repair_text}"
                            ),
                            "text_elements": [],
                        }
                    ],
                },
            )
            turn = result.get("turn") or {}
            self.turn_id = str(turn.get("id") or self.turn_id or "") or None
            self.store.update(self.id, turn_id=self.turn_id)
            self._new.set()
        except Exception as exc:
            self._finish("error", error=f"Could not continue Codex for handoff: {exc}")

    def _deployment_guard_issue(self) -> str | None:
        if not self.enable_deploy:
            return None
        root = find_deployment_root(self.workdir)
        if root is None:
            return (
                "No aios.deploy.yaml was found in the working directory or its "
                "ancestors. Create the manifest at the app root and perform the "
                "matching AIOS MCP deployments."
            )
        try:
            manifest = load_deployment_manifest(root)
        except ManifestValidationError as exc:
            return f"The deployment manifest is invalid: {exc}"
        required = {
            tool
            for component, tool in _DEPLOY_TOOL_BY_COMPONENT.items()
            if getattr(manifest, component) is not None
        }
        with self._lock:
            uncalled = sorted(required - self._deploy_tools_called)
            not_enqueued = sorted(required - self._deploy_tools_enqueued)
            last_results = {
                tool: self._deploy_last_results.get(tool) for tool in not_enqueued
            }
        if not not_enqueued:
            return None
        if uncalled:
            detail = f"were not made: {', '.join(uncalled)}"
        else:
            detail = "did not return a deployment ID: " + ", ".join(not_enqueued)
            detail += ". Last AIOS responses: " + json.dumps(
                last_results, default=str, sort_keys=True
            )
        return (
            f"Manifest {root / 'aios.deploy.yaml'} requires these AIOS MCP calls "
            f"that {detail}. Call them now in database, "
            "server, frontend dependency order and report their exact responses."
        )

    def _persist_deploy_state(self) -> None:
        with self._lock:
            deploy_state = {
                "called": sorted(self._deploy_tools_called),
                "enqueued": sorted(self._deploy_tools_enqueued),
                "followups": self._deploy_followups,
                "last_results": dict(self._deploy_last_results),
            }
        self.store.update(
            self.id,
            deploy_state=deploy_state,
        )

    def _start_deploy_followup(self, issue: str) -> None:
        try:
            thread_id = self.thread_id
            if not thread_id:
                raise RuntimeError("Codex thread id is unavailable")
            with self._lock:
                self._final_message = None
            result = self._rpc(
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": [
                        {
                            "type": "text",
                            "text": (
                                "The host rejected completion because the mandatory "
                                f"AIOS deployment postcondition is unmet. {issue} "
                                "Continue in this same workspace. Do not merely explain "
                                "what should be called: make the required deploy MCP calls."
                            ),
                            "text_elements": [],
                        }
                    ],
                },
            )
            turn = result.get("turn") or {}
            self.turn_id = str(turn.get("id") or self.turn_id or "") or None
            self.store.update(self.id, turn_id=self.turn_id)
            self._new.set()
        except Exception as exc:
            self._finish(
                "error", error=f"Could not continue Codex for deployment: {exc}"
            )

    def _finish(
        self, status: str, *, error: str | None = None, result: str | None = None
    ) -> None:
        with self._lock:
            if self.status in _TERMINAL_STATUSES or self._finishing:
                return
            self._finishing = True
            pending_calls = list(self._pending_rpc.values())
        self._terminate_live_process()
        if (
            status in {"error", "cancelled"}
            and self.app_contract is not None
            and self.app_contract.get("deployment_requested")
        ):
            try:
                if self.worktree_registry is not None:
                    self.worktree_registry.abandon_codex_handoff(
                        str(self.app_contract["worktree_id"]),
                        owner_job_id=self.id,
                        error=error or status,
                    )
            except (KeyError, WorktreeHandoffError, OSError):
                log.exception("Failed to reclaim Codex worktree for %s", self.id)
        persistence_error: Exception | None = None
        terminal_persisted = False
        try:
            self.store.update(
                self.id,
                status=status,
                clear_pending_input=True,
                result=result,
                error=error,
                terminal=True,
                clear_process=True,
            )
            terminal_persisted = True
            if status in {"done", "error"}:
                self.store.enqueue_signal(self.id, status)
        except Exception as exc:
            persistence_error = exc
            log.exception("Failed to persist terminal Codex state for %s", self.id)
        if (
            terminal_persisted
            and self.app_contract is not None
            and self.worktree_registry is not None
        ):
            try:
                self.worktree_registry.release_app_lease(
                    app_id=str(self.app_contract["app_id"]),
                    owner_job_id=self.id,
                )
            except (KeyError, WorktreeHandoffError, OSError):
                log.exception("Failed to release app lease for %s", self.id)
        for pending in pending_calls:
            pending.ready.set()
        self._new.set()
        self._emit(
            "codex.completed", {"status": status, "result": result, "error": error}
        )
        if status in {"done", "error"}:
            self._emit_lifecycle(status)
        log.info(
            "Codex job finished",
            extra={
                "codex_job_id": self.id,
                "codex_session_id": self.session_id,
                "codex_status": status,
                "codex_duration_ms": int((monotonic() - self.started_at) * 1000),
            },
        )
        with self._lock:
            self.status = status
            self.error = error
            self.result = result
            self.finished_at = monotonic()
            self._pending_input = None
            self._pending_input_request_id = None
            self._finishing = False
        if persistence_error is not None:
            self.error = (
                self.error or f"failed to persist terminal state: {persistence_error}"
            )

    def _terminate_live_process(self) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        try:
            pid = getattr(proc, "pid", None)
            if isinstance(pid, int):
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            else:
                proc.terminate()
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _watchdog(self) -> None:
        deadline = self.started_at + SAFETY_CAP_SECONDS
        while True:
            with self._lock:
                if self.status in _TERMINAL_STATUSES or self._finishing:
                    return
            remaining = deadline - monotonic()
            if remaining <= 0:
                self._finish(
                    "error", error=f"Codex exceeded safety cap {SAFETY_CAP_SECONDS:g}s"
                )
                return
            self._new.wait(timeout=min(1.0, remaining))
            self._new.clear()

    def poll(self, cursor: int = 0, wait: float = 0.0) -> dict[str, Any]:
        cursor = max(0, int(cursor))
        if wait and wait > 0:
            end = monotonic() + float(wait)
            while monotonic() < end:
                events, _ = self.store.events_after(self.id, cursor)
                with self._lock:
                    ready = bool(events) or self.status != "running"
                if ready:
                    break
                self._new.wait(timeout=min(0.5, max(0.0, end - monotonic())))
                self._new.clear()
        events, next_cursor = self.store.events_after(self.id, cursor)
        with self._lock:
            return {
                "job_id": self.id,
                "status": self.status,
                "thread_id": self.thread_id,
                "turn_id": self.turn_id,
                "events": events,
                "cursor": next_cursor,
                "pending_input": self._pending_input,
                "result": self.result if self.status == "done" else None,
                "workspace_handoff": self.workspace_handoff
                if self.status == "done"
                else None,
                "error": self.error,
            }

    def stop(self) -> None:
        thread_id, turn_id = self.thread_id, self.turn_id
        if thread_id and turn_id and self.status in _ACTIVE_STATUSES:
            try:
                self._rpc("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
            except Exception:
                pass
        self._finish("cancelled", error="stopped by request")

    def interrupt_for_restart(self) -> None:
        """Stop the local process while leaving its durable run recoverable."""

        with self._lock:
            if self.status in _TERMINAL_STATUSES or self._finishing:
                return
            self._finishing = True
            pending_calls = list(self._pending_rpc.values())
        self._terminate_live_process()
        try:
            self.store.update(
                self.id,
                status=self.status,
                error="Host restart interrupted the local Codex process; recovery pending",
                clear_process=True,
            )
        except Exception:
            log.exception("Failed to persist restart interruption for %s", self.id)
        for pending in pending_calls:
            pending.ready.set()
        self._new.set()

    def summary(self) -> dict[str, Any]:
        with self._lock:
            return {
                "job_id": self.id,
                "session_id": self.session_id,
                "status": self.status,
                "task": self.task[:80],
                "events": len(self.events),
                "pending_input": self._pending_input,
            }


class CodexJobManager:
    def __init__(
        self,
        store: CodexRunStore | None = None,
        *,
        worktree_registry: WorktreeRegistry | None = None,
    ) -> None:
        self._jobs: dict[str, CodexJob] = {}
        self._lock = threading.Lock()
        self.store = store or CodexRunStore(":memory:")
        self._worktree_registry = worktree_registry
        self._shutting_down = False

    def _registry(self) -> WorktreeRegistry:
        if self._worktree_registry is None:
            self._worktree_registry = WorktreeRegistry()
        return self._worktree_registry

    @staticmethod
    def _command(enable_deploy: bool) -> list[str]:
        cmd = [
            "codex",
            "app-server",
            "--stdio",
            "--enable",
            "default_mode_request_user_input",
        ]
        if enable_deploy:
            cmd.extend(["-c", _deploy_mcp_config()])
        return cmd

    def start(
        self,
        task: str,
        path: str = ".",
        model: str | None = None,
        enable_deploy: bool = False,
        session_id: str | None = None,
        parent_tool_call_id: str | None = None,
        parent_run_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(task, str) or not task.strip():
            return {"error": "task is required"}
        if not isinstance(path, str) or not path.strip():
            return {"error": "path must be a non-empty string"}
        try:
            workdir = resolve_chat_files_path(path.strip()).resolve()
        except ValueError as exc:
            return {"error": str(exc)}
        if not workdir.exists():
            return {"error": f"path does not exist: {workdir}"}
        if not workdir.is_dir():
            return {"error": f"path is not a directory: {workdir}"}

        job_id = uuid4().hex[:12]
        app_contract: dict[str, Any] | None = None
        lease_acquired = False
        reservation_worktree_id: str | None = None
        registry = self._worktree_registry
        if registry is None:
            default_apps = (get_workspace_dir() / "apps").resolve()
            try:
                workdir.relative_to(default_apps)
            except ValueError:
                pass
            else:
                registry = self._registry()
        registered_app = (
            _find_registered_app_root(workdir, registry)
            if registry is not None
            else None
        )
        if enable_deploy and registered_app is None:
            return {
                "error": (
                    "app deployment preflight failed: path is not a registered "
                    "canonical workspace/apps/<app-id> root"
                )
            }
        if registered_app is not None:
            workdir, app_id = registered_app
            try:
                registry.acquire_app_lease(
                    app_id=app_id,
                    repository=workdir,
                    owner_job_id=job_id,
                )
                lease_acquired = True
                app_contract = _prepare_app_contract(
                    app_root=workdir,
                    app_id=app_id,
                    job_id=job_id,
                    deployment_requested=enable_deploy,
                )
                if enable_deploy:
                    # ``deploy`` is an intent flag. Codex prepares source and a
                    # registered worktree; it never receives cloud capabilities.
                    reservation = registry.reserve(
                        app_id=app_id,
                        repository=workdir,
                        owner_job_id=job_id,
                        purpose="prepare_deployment_source",
                        display_name=f"deploy-{job_id}",
                    )
                    reservation_worktree_id = reservation.worktree_id
                    app_contract.update(
                        {
                            "handoff_id": reservation.handoff_id,
                            "worktree_id": reservation.worktree_id,
                            "workspace_path": reservation.path,
                        }
                    )
                    effective_task = _app_change_contract_task(task, app_contract)
                else:
                    effective_task = _app_record_contract_task(task, app_contract)
            except (
                AppGitError,
                WorktreeHandoffError,
                ManifestValidationError,
                OSError,
            ) as exc:
                if reservation_worktree_id is not None:
                    try:
                        registry.abandon_codex_handoff(
                            reservation_worktree_id,
                            owner_job_id=job_id,
                            error=f"app preflight failed: {exc}",
                        )
                    except (WorktreeHandoffError, OSError):
                        log.exception(
                            "Failed to reclaim reservation for rejected job %s", job_id
                        )
                if lease_acquired:
                    try:
                        registry.release_app_lease(
                            app_id=app_id, owner_job_id=job_id
                        )
                    except (WorktreeHandoffError, OSError):
                        log.exception(
                            "Failed to release lease for rejected job %s", job_id
                        )
                label = "deployment" if enable_deploy else "change"
                return {"error": f"app {label} preflight failed: {exc}"}
        else:
            effective_task = task.strip()

        cmd = self._command(False)
        job = CodexJob(
            job_id,
            effective_task,
            str(workdir),
            cmd,
            model=model.strip() if isinstance(model, str) and model.strip() else None,
            session_id=session_id,
            parent_tool_call_id=parent_tool_call_id,
            store=self.store,
            enable_deploy=False,
            app_contract=app_contract,
            worktree_registry=registry if app_contract is not None else None,
            unexpected_exit_sink=self._handle_unexpected_exit,
        )
        with self._lock:
            durable_active = {
                str(record["job_id"]): record for record in self.store.active()
            }
            live_active = {
                jid: item
                for jid, item in self._jobs.items()
                if item.status in _ACTIVE_STATUSES
            }
            active_ids = set(durable_active) | set(live_active)
            if len(active_ids) >= MAX_ACTIVE_JOBS:
                self._cleanup_rejected_start(
                    job_id=job_id,
                    app_contract=app_contract,
                    registry=registry,
                    reservation_worktree_id=reservation_worktree_id,
                    error="Codex job capacity was reached",
                )
                return {
                    "error": (
                        f"too many active codex jobs ({MAX_ACTIVE_JOBS}); "
                        f"running: {sorted(active_ids)}"
                    )
                }
            conflicting = next(
                (
                    item.id
                    for item in live_active.values()
                    if item.workdir == str(workdir)
                ),
                None,
            )
            if conflicting is None:
                conflicting = next(
                    (
                        job_id
                        for job_id, record in durable_active.items()
                        if record.get("workdir") == str(workdir)
                    ),
                    None,
                )
            if conflicting:
                self._cleanup_rejected_start(
                    job_id=job_id,
                    app_contract=app_contract,
                    registry=registry,
                    reservation_worktree_id=reservation_worktree_id,
                    error=f"Codex job {conflicting} already owns this workspace",
                )
                return {
                    "error": (
                        f"Codex job {conflicting} is already editing {workdir}; "
                        "wait for it to finish or stop it before starting another"
                    )
                }
            self._jobs[job_id] = job
            try:
                self.store.create(
                    job_id=job_id,
                    session_id=session_id,
                    parent_run_id=parent_run_id,
                    parent_tool_call_id=parent_tool_call_id,
                    task=effective_task,
                    workdir=str(workdir),
                    model=job.model,
                    capabilities=["filesystem", "shell"]
                    + (
                        ["deployment_handoff_v3"]
                        if enable_deploy
                        else ["app_change_v3"]
                        if app_contract is not None
                        else []
                    ),
                    contract_version=3 if app_contract is not None else 1,
                    deployment_requested=enable_deploy,
                    app_state=app_contract,
                )
            except Exception as exc:
                self._jobs.pop(job_id, None)
                self._cleanup_rejected_start(
                    job_id=job_id,
                    app_contract=app_contract,
                    registry=registry,
                    reservation_worktree_id=reservation_worktree_id,
                    error=f"failed to persist Codex job: {exc}",
                )
                return {"error": f"failed to persist codex job -- {exc}"}
        try:
            job.start()
        except FileNotFoundError:
            with self._lock:
                self._jobs.pop(job_id, None)
            self.store.update(
                job_id,
                status="error",
                error="codex CLI is not installed or not on PATH",
                terminal=True,
            )
            self._cleanup_rejected_start(
                job_id=job_id,
                app_contract=app_contract,
                registry=registry,
                reservation_worktree_id=reservation_worktree_id,
                error="codex CLI is unavailable",
            )
            return {"error": "codex CLI is not installed or not on PATH"}
        except Exception as exc:
            with self._lock:
                self._jobs.pop(job_id, None)
            self.store.update(job_id, status="error", error=str(exc), terminal=True)
            self._cleanup_rejected_start(
                job_id=job_id,
                app_contract=app_contract,
                registry=registry,
                reservation_worktree_id=reservation_worktree_id,
                error=f"failed to start Codex: {exc}",
            )
            return {"error": f"failed to start codex -- {exc}"}
        response = {
            "job_id": job_id,
            "status": "running",
            "workdir": str(workdir),
            "auto_continuation": bool(session_id),
        }
        return response

    @staticmethod
    def _cleanup_rejected_start(
        *,
        job_id: str,
        app_contract: dict[str, Any] | None,
        registry: WorktreeRegistry | None,
        reservation_worktree_id: str | None,
        error: str,
    ) -> None:
        if registry is None or app_contract is None:
            return
        if reservation_worktree_id is not None:
            try:
                registry.abandon_codex_handoff(
                    reservation_worktree_id,
                    owner_job_id=job_id,
                    error=error,
                )
            except (WorktreeHandoffError, OSError):
                log.exception("Failed to reclaim rejected Codex handoff %s", job_id)
        try:
            registry.release_app_lease(
                app_id=str(app_contract["app_id"]), owner_job_id=job_id
            )
        except (KeyError, WorktreeHandoffError, OSError):
            log.exception("Failed to release rejected Codex app lease %s", job_id)

    def get(self, job_id: str) -> CodexJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def poll(
        self,
        job_id: str,
        cursor: int = 0,
        wait: float = 0.0,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        job = self.get(job_id)
        if job is not None:
            if session_id is not None and job.session_id != session_id:
                return {"error": f"unknown job_id: {job_id}"}
            result = job.poll(cursor=cursor, wait=min(max(float(wait), 0.0), 30.0))
            record = self.store.get(job_id) or {}
            for key in (
                "display_status",
                "recovery_count",
                "verification_status",
                "created_at",
                "updated_at",
            ):
                if key in record:
                    result[key] = record[key]
            return result
        record = self.store.get(job_id)
        if record is None or (
            session_id is not None and record.get("session_id") != session_id
        ):
            return {"error": f"unknown job_id: {job_id}"}
        events, next_cursor = self.store.events_after(job_id, cursor)
        return {
            **record,
            "events": events,
            "cursor": next_cursor,
        }

    def answer(
        self,
        job_id: str,
        answers: dict[str, Any],
        session_id: str | None = None,
    ) -> dict[str, Any]:
        job = self.get(job_id)
        if job is not None:
            if session_id is not None and job.session_id != session_id:
                return {"error": f"unknown job_id: {job_id}"}
            return job.answer(answers)
        record = self.store.get(job_id)
        if (
            record is None
            or (session_id is not None and record.get("session_id") != session_id)
            or record.get("status") != "awaiting_input"
        ):
            return {"error": f"unknown job_id: {job_id}"}
        pending = record.get("pending_input") or {}
        expected = {
            str(question.get("id"))
            for question in pending.get("questions", [])
            if isinstance(question, dict) and question.get("id")
        }
        missing = sorted(expected - {str(key) for key in answers})
        if missing:
            return {"error": f"missing answers for: {', '.join(missing)}"}
        prompt = (
            "The server restarted while you were waiting for input. Resume the "
            "delegated task from the current workspace and thread. The user supplied "
            f"these answers to your prior questions: {json.dumps(answers, default=str)}. "
            "Inspect existing work before acting, avoid repeating completed external "
            "side effects, finish the task, and report verification performed."
        )
        self._terminate_recorded_process(record)
        return self._recover_record(record, recovery_prompt=prompt)

    def stop(self, job_id: str, session_id: str | None = None) -> dict[str, Any]:
        job = self.get(job_id)
        if job is not None:
            if session_id is not None and job.session_id != session_id:
                return {"error": f"unknown job_id: {job_id}"}
            job.stop()
            return {"job_id": job_id, "status": job.status}
        record = self.store.get(job_id)
        if (
            record is None
            or (session_id is not None and record.get("session_id") != session_id)
            or record.get("status") not in _ACTIVE_STATUSES
        ):
            return {"error": f"unknown job_id: {job_id}"}
        self._terminate_recorded_process(record)
        self.store.update(
            job_id,
            status="cancelled",
            error="stopped by request",
            terminal=True,
            clear_process=True,
            clear_pending_input=True,
        )
        self._release_record_resources(record, error="stopped by request")
        self.emit_status(
            job_id,
            "codex.completed",
            {"status": "cancelled", "error": "stopped by request", "result": None},
        )
        return {"job_id": job_id, "status": "cancelled"}

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [job.summary() for job in self._jobs.values()]

    def list_for_session(self, session_id: str) -> list[dict[str, Any]]:
        return self.store.list_for_session(session_id)

    def stop_for_session(self, session_id: str) -> list[str]:
        with self._lock:
            jobs = [
                job
                for job in self._jobs.values()
                if job.session_id == session_id and job.status in _ACTIVE_STATUSES
            ]
        for job in jobs:
            job.stop()
        stopped = [job.id for job in jobs]
        live_ids = set(stopped)
        for record in self.store.active():
            job_id = str(record["job_id"])
            if record.get("session_id") == session_id and job_id not in live_ids:
                result = self.stop(job_id, session_id=session_id)
                if "error" not in result:
                    stopped.append(job_id)
        return stopped

    def stop_all(self) -> list[str]:
        with self._lock:
            jobs = [
                job for job in self._jobs.values() if job.status in _ACTIVE_STATUSES
            ]
        for job in jobs:
            job.stop()
        stopped = [job.id for job in jobs]
        live_ids = set(stopped)
        for record in self.store.active():
            job_id = str(record["job_id"])
            if job_id not in live_ids:
                result = self.stop(job_id)
                if "error" not in result:
                    stopped.append(job_id)
        return stopped

    def interrupt_all_for_restart(self) -> list[str]:
        """Terminate local Codex processes without cancelling durable runs."""

        with self._lock:
            self._shutting_down = True
            jobs = [
                job for job in self._jobs.values() if job.status in _ACTIVE_STATUSES
            ]
        for job in jobs:
            job.interrupt_for_restart()
        interrupted = [job.id for job in jobs]
        live_ids = set(interrupted)
        for record in self.store.active():
            job_id = str(record["job_id"])
            if job_id in live_ids:
                continue
            self._terminate_recorded_process(record)
            self.store.update(job_id, clear_process=True)
            interrupted.append(job_id)
        return interrupted

    def _handle_unexpected_exit(self, job: CodexJob, detail: str) -> None:
        """Claim one live crash and recover it without waking the orchestrator."""

        with self._lock:
            if self._shutting_down or self._jobs.get(job.id) is not job:
                return
            self._jobs.pop(job.id, None)
        record = self.store.get(job.id)
        if record is None or record.get("status") not in _ACTIVE_STATUSES:
            return
        self.store.update(
            job.id,
            status="running",
            error=f"Codex child exited unexpectedly: {detail}",
            clear_process=True,
            clear_pending_input=True,
        )
        event = {
            "kind": "process_recovery",
            "phase": "scheduled",
            "detail": detail,
            "attempt": int(record.get("recovery_count") or 0) + 1,
        }
        self.store.append_event(job.id, event)
        self.emit_status(job.id, "codex.recovery.scheduled", event)
        threading.Thread(
            target=self._recover_after_process_exit,
            args=(job.id, detail),
            daemon=True,
        ).start()

    def _recover_after_process_exit(self, job_id: str, detail: str) -> None:
        """Retry replacement process launches until the durable limit is exhausted."""

        while True:
            with self._lock:
                if self._shutting_down:
                    return
            record = self.store.get(job_id)
            if record is None or record.get("status") not in _ACTIVE_STATUSES:
                return
            attempts = int(record.get("recovery_count") or 0)
            if attempts >= MAX_RECOVERY_ATTEMPTS:
                self._fail_recovery(
                    job_id,
                    "Codex child-process recovery was exhausted after "
                    f"{attempts} attempts. Last failure: {detail}",
                )
                return
            delay = min(
                2.0,
                max(0.0, RECOVERY_BACKOFF_BASE_SECONDS) * (2**attempts),
            )
            if delay:
                sleep(delay)
            with self._lock:
                if self._shutting_down:
                    return
            record = self.store.get(job_id)
            if record is None or record.get("status") not in _ACTIVE_STATUSES:
                return
            result = self._recover_record(
                record,
                recovery_prompt=(
                    "The Codex child process exited unexpectedly while the host stayed "
                    "alive. Resume the original task from durable workspace state. "
                    "Inspect existing work before acting, do not repeat completed "
                    "side effects, and finish verification and the required handoff. "
                    f"Observed process failure: {detail}"
                ),
                allow_fresh_thread=True,
                terminal_on_start_failure=False,
            )
            if "error" not in result:
                return
            detail = str(result["error"])

    def reconcile_stale(self) -> list[str]:
        recovered: list[str] = []
        live_ids = set(self._jobs)
        active_records = self.store.active()
        active_ids = {str(record["job_id"]) for record in active_records}
        default_registry_root = (get_workspace_dir() / ".aios" / "worktrees").resolve()
        registry = self._worktree_registry
        if registry is None and (
            (default_registry_root / "leases").exists()
            or any(record.get("app_state") for record in active_records)
        ):
            registry = self._registry()
        if registry is not None:
            registry.reconcile_app_leases(active_codex_job_ids=active_ids)
        for record in active_records:
            job_id = str(record["job_id"])
            if job_id in live_ids:
                continue
            app_state = dict(record.get("app_state") or {})
            if app_state:
                try:
                    self._registry().acquire_app_lease(
                        app_id=str(app_state["app_id"]),
                        repository=str(app_state["app_root"]),
                        owner_job_id=job_id,
                    )
                except (KeyError, WorktreeHandoffError, OSError) as exc:
                    self._fail_recovery(
                        job_id, f"Could not reacquire the app mutation lease: {exc}"
                    )
                    continue
            self._terminate_recorded_process(record)
            if self._finalize_ready_handoff(record):
                recovered.append(job_id)
                continue
            if record.get("status") == "awaiting_input":
                self.store.update(job_id, clear_process=True)
                self.store.enqueue_signal(job_id, "awaiting_input")
                continue
            result = self._recover_record(record)
            if "error" not in result:
                recovered.append(job_id)
        return recovered

    def _finalize_ready_handoff(self, record: dict[str, Any]) -> bool:
        """Finish a run whose validated handoff outlived its process/DB update."""

        app_state = dict(record.get("app_state") or {})
        if not app_state.get("deployment_requested") or not app_state.get(
            "worktree_id"
        ):
            return False
        registry = self._registry()
        try:
            handoff = registry.get(str(app_state["worktree_id"]))
        except WorktreeHandoffError:
            return False
        job_id = str(record["job_id"])
        if (
            handoff.owner_job_id != job_id
            or handoff.status != WorktreeStatus.HANDOFF_READY
        ):
            return False
        workspace_handoff = self._workspace_handoff_payload(
            handoff,
            contract_version=int(record.get("contract_version") or 1),
            mode=app_state.get("completion_mode"),
        )
        app_state.update(
            {
                "source_commit": handoff.source_commit,
                "source_tree": handoff.source_tree,
                "provenance_commit": handoff.provenance_commit,
                "workspace_handoff": workspace_handoff,
            }
        )
        result = str(
            record.get("result")
            or "Codex completed and published a validated workspace handoff."
        )
        self.store.update(
            job_id,
            status="done",
            result=result,
            error="",
            app_state=app_state,
            terminal=True,
            clear_process=True,
            clear_pending_input=True,
        )
        self.store.enqueue_signal(job_id, "done")
        try:
            registry.release_app_lease(
                app_id=str(app_state["app_id"]), owner_job_id=job_id
            )
        except (KeyError, WorktreeHandoffError, OSError):
            log.exception("Failed to release recovered app lease for %s", job_id)
        self.emit_status(
            job_id,
            "codex.completed",
            {"status": "done", "result": result, "error": None},
        )
        log.info("Finalized already-published Codex handoff for %s", job_id)
        return True

    @staticmethod
    def _workspace_handoff_payload(
        handoff: WorktreeRecord,
        *,
        contract_version: int,
        mode: object = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "handoff_id": handoff.handoff_id,
            "worktree_id": handoff.worktree_id,
            "app_id": handoff.app_id,
            "canonical_repository": handoff.repository,
            "workspace_path": handoff.path,
            "source_commit": handoff.source_commit,
            "source_tree": handoff.source_tree,
            "provenance_commit": handoff.provenance_commit,
            "selection_reason": handoff.selection_reason,
            "status": handoff.status,
            "contract_version": contract_version,
        }
        if isinstance(mode, str) and mode:
            payload["mode"] = mode
        return payload

    def _recover_record(
        self,
        record: dict[str, Any],
        *,
        recovery_prompt: str | None = None,
        allow_fresh_thread: bool = False,
        terminal_on_start_failure: bool = True,
    ) -> dict[str, Any]:
        job_id = str(record["job_id"])
        if self._finalize_ready_handoff(record):
            return {
                "job_id": job_id,
                "status": "done",
                "recovered": True,
                "workspace_handoff": (
                    self.store.get(job_id) or {}
                ).get("workspace_handoff"),
            }
        thread_id = record.get("thread_id")
        attempts = int(record.get("recovery_count") or 0)
        if not thread_id and not allow_fresh_thread:
            return self._fail_recovery(job_id, "Codex thread id was not persisted.")
        if attempts >= MAX_RECOVERY_ATTEMPTS:
            return self._fail_recovery(
                job_id,
                f"Codex exceeded {MAX_RECOVERY_ATTEMPTS} recovery attempts.",
            )
        prompt = recovery_prompt or (
            "The host server restarted during this delegated task. Resume from the "
            "current workspace and existing thread. Inspect what is already complete, "
            "avoid repeating completed external side effects, finish the original task, "
            "and run proportionate verification before reporting. Original task: "
            f"{record.get('task')}"
        )
        capabilities = list(record.get("capabilities") or [])
        legacy_cloud_deploy = "cloud_deploy" in capabilities
        app_contract = (
            dict(record.get("app_state") or {})
            if {
                "deployment_handoff_v2",
                "app_change_v2",
                "deployment_handoff_v3",
                "app_change_v3",
            }.intersection(capabilities)
            else None
        )
        if app_contract is not None:
            try:
                self._registry().acquire_app_lease(
                    app_id=str(app_contract["app_id"]),
                    repository=str(app_contract["app_root"]),
                    owner_job_id=job_id,
                )
            except (KeyError, WorktreeHandoffError, OSError) as exc:
                return self._fail_recovery(
                    job_id, f"Could not reacquire the app mutation lease: {exc}"
                )
        job = CodexJob(
            job_id,
            str(record.get("task") or ""),
            str(record.get("workdir") or "."),
            self._command(legacy_cloud_deploy),
            model=record.get("model"),
            session_id=record.get("session_id"),
            parent_tool_call_id=record.get("parent_tool_call_id"),
            store=self.store,
            enable_deploy=legacy_cloud_deploy,
            deploy_state=record.get("deploy_state"),
            app_contract=app_contract,
            worktree_registry=self._registry() if app_contract else None,
            unexpected_exit_sink=self._handle_unexpected_exit,
            resume_thread_id=str(thread_id) if thread_id else None,
            recovery_prompt=prompt,
        )
        with self._lock:
            existing = self._jobs.get(job_id)
            if existing is not None and existing.status in _ACTIVE_STATUSES:
                return {"job_id": job_id, "status": existing.status}
            self._jobs[job_id] = job
        self.store.update(
            job_id,
            status="running",
            clear_pending_input=True,
            clear_process=True,
            recovery_count=attempts + 1,
            error="",
        )
        try:
            job.start()
        except Exception as exc:
            with self._lock:
                self._jobs.pop(job_id, None)
            message = f"Failed to restart Codex: {exc}"
            if terminal_on_start_failure:
                return self._fail_recovery(job_id, message)
            self.store.update(
                job_id,
                status="running",
                error=message,
                clear_process=True,
            )
            event = {
                "kind": "process_recovery",
                "phase": "launch_failed",
                "detail": message,
                "attempt": attempts + 1,
            }
            self.store.append_event(job_id, event)
            self.emit_status(job_id, "codex.recovery.failed", event)
            return {
                "job_id": job_id,
                "status": "running",
                "error": message,
                "recovered": False,
            }
        log.info("Recovered Codex job %s on thread %s", job_id, thread_id)
        return {"job_id": job_id, "status": "running", "recovered": True}

    def _fail_recovery(self, job_id: str, message: str) -> dict[str, Any]:
        record = self.store.get(job_id)
        if record is not None:
            self._release_record_resources(record, error=message)
        self.store.update(
            job_id,
            status="error",
            error=message,
            terminal=True,
            clear_process=True,
        )
        self.store.enqueue_signal(job_id, "error")
        self.emit_status(
            job_id,
            "codex.completed",
            {"status": "error", "error": message, "result": None},
        )
        record = self.store.get(job_id)
        if (
            _lifecycle_sink is not None
            and record is not None
            and record.get("session_id")
        ):
            try:
                _lifecycle_sink(str(record["session_id"]), job_id, "error")
            except Exception:
                log.exception("Failed to submit Codex recovery failure continuation")
        log.error("Could not recover Codex job %s: %s", job_id, message)
        return {"job_id": job_id, "status": "error", "error": message}

    def _release_record_resources(
        self, record: dict[str, Any], *, error: str
    ) -> None:
        app_contract = dict(record.get("app_state") or {})
        if not app_contract:
            return
        registry = self._registry()
        job_id = str(record["job_id"])
        if app_contract.get("deployment_requested") and app_contract.get(
            "worktree_id"
        ):
            try:
                registry.abandon_codex_handoff(
                    str(app_contract["worktree_id"]),
                    owner_job_id=job_id,
                    error=error,
                )
            except (WorktreeHandoffError, OSError):
                log.exception("Failed to reclaim Codex worktree for %s", job_id)
        try:
            registry.release_app_lease(
                app_id=str(app_contract["app_id"]), owner_job_id=job_id
            )
        except (KeyError, WorktreeHandoffError, OSError):
            log.exception("Failed to release app lease for %s", job_id)

    @staticmethod
    def _terminate_recorded_process(record: dict[str, Any]) -> bool:
        pid = record.get("process_pid")
        expected = record.get("process_identity")
        if not isinstance(pid, int) or not isinstance(expected, str) or not expected:
            return False
        actual = _process_identity(pid)
        if actual != expected or "codex" not in actual.lower():
            return False
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            return False

    def emit_status(
        self, job_id: str, event_type: str, payload: dict[str, Any]
    ) -> None:
        record = self.store.get(job_id)
        if record is None or not record.get("session_id"):
            return
        sequence = self.store.append_gateway_event(
            job_id,
            str(record["session_id"]),
            event_type,
            {"job_id": job_id, **payload},
        )
        delivered_payload = {
            "job_id": job_id,
            **payload,
            "codex_event_id": f"{job_id}:{sequence}",
        }
        if _progress_sink is not None:
            try:
                _progress_sink(
                    str(record["session_id"]),
                    event_type,
                    delivered_payload,
                )
                self.store.complete_gateway_event(job_id, sequence)
            except Exception:
                log.exception("Failed to publish Codex status event %s", event_type)

    def cleanup(self) -> int:
        return self.store.cleanup(RETENTION_DAYS)

    def metrics(self) -> dict[str, Any]:
        result = self.store.metrics()
        with self._lock:
            result["live_jobs"] = sum(
                1 for job in self._jobs.values() if job.status in _ACTIVE_STATUSES
            )
        return result


_manager = CodexJobManager(CodexRunStore())


def codex_start(
    task: str | None = None,
    path: str = ".",
    model: str | None = None,
    deploy: bool = False,
    fc=None,
) -> dict[str, Any]:
    """Start Codex; deploy intent requests a source handoff, never Codex cloud access."""
    return _manager.start(
        task or "",
        path=path,
        model=model,
        enable_deploy=bool(deploy),
        session_id=get_current_chat_id(),
        parent_run_id=get_current_run_id(),
        parent_tool_call_id=str(getattr(fc, "call_id", None))
        if getattr(fc, "call_id", None)
        else None,
    )


def codex_poll(
    job_id: str | None = None, cursor: int = 0, wait: float = 0.0, fc=None
) -> dict[str, Any]:
    """Poll a job. ``awaiting_input`` includes a structured ``pending_input``."""
    return _manager.poll(
        job_id or "", cursor=cursor, wait=wait, session_id=get_current_chat_id()
    )


def codex_answer(
    job_id: str | None = None, answers: dict[str, Any] | None = None, fc=None
) -> dict[str, Any]:
    """Answer the questions in a Codex job's ``pending_input`` object."""
    return _manager.answer(
        job_id or "", answers or {}, session_id=get_current_chat_id()
    )


def codex_stop(job_id: str | None = None, fc=None) -> dict[str, Any]:
    """Interrupt and stop a Codex job."""
    return _manager.stop(job_id or "", session_id=get_current_chat_id())
