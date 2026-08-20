from __future__ import annotations

from aios_core.agent_prompt import build_agent_prompt


def _prompt() -> str:
    return build_agent_prompt(
        include_subagent_tool=False,
        include_memory_tools=False,
        default_cron_timezone="UTC",
        workspace_dir="/tmp/workspace",
    )


def test_prompt_routes_every_code_or_app_task_to_codex() -> None:
    prompt = _prompt()

    assert "HARD CODEX ROUTING GATE — NON-OPTIONAL" in prompt
    assert "even remotely about code" in prompt
    assert "must not substitute `glob`, `grep`, `read`, `bash`" in prompt
    assert "call `codex_start` in that same turn" in prompt
    assert "explicit Codex request overrides" in prompt
    assert "including incomplete device-only apps" in prompt
    assert "Do not use the cloud app inventory" in prompt
    assert "independently inspect Codex's diff" in prompt


def test_prompt_requires_native_tool_calls_and_runtime_issued_job_ids() -> None:
    prompt = _prompt()

    assert "NATIVE TOOL-CALL INTEGRITY — NON-OPTIONAL" in prompt
    assert "copying that syntax does not execute anything" in prompt
    assert "only after its actual structured tool result" in prompt
    assert "Never invent, guess, transform, or manually reconstruct a job ID" in prompt
    assert "no verified Codex job was started" in prompt


def test_prompt_routes_app_work_to_durable_workspace() -> None:
    prompt = _prompt()

    assert "/tmp/workspace/apps/<app-id>" in prompt
    assert '"app_workspace": (' in prompt
    assert "Never fabricate replacement source" in prompt
    assert "path` set exactly to the returned `workspace_path`" in prompt


def test_prompt_routes_deployment_through_artifact_handoff() -> None:
    prompt = _prompt()

    assert "MAIN-AGENT DEPLOYMENT" in prompt
    assert "Codex owns code changes" in prompt
    assert "it never deploys" in prompt
    assert "`create_app_artifact`" in prompt
    assert "`prepare_app_route`" in prompt
    assert "`deploy_app_artifact`" in prompt
    assert "forward its exact `route_id`" in prompt
    assert "never exposes or authorizes a reserved handoff" in prompt
    assert "`workspace_handoff` with `status=handoff_ready`" in prompt
    assert "pass only that result's exact `handoff_id`" in prompt
    assert "never pass app paths or revisions into artifact creation" in prompt
    assert "using only that artifact's exact `artifact_id`" in prompt
    assert "Downstream tools resolve the app and component list" in prompt
    assert "never call `write`, `edit`, shell/process tools" in prompt
    assert "artifact_verified=true" in prompt
    assert "provisioning_status=provisioned" in prompt
    assert "DEPLOYMENT STATUS CALL SEQUENCE — MANDATORY" in prompt
    assert "Call `deployment_status` for every component deployment ID" in prompt
    assert (
        "call `deployment_events` for each deployment"
        in prompt
    )
    assert "continue bounded status and event checks" in prompt
    assert (
        "call `activate_app_route` only after the pipeline and every public component are active"
        in prompt
    )
    assert "then call `app_route_status` with the exact app and route IDs" in prompt


def test_prompt_distinguishes_git_cleanliness_from_worktree_cleanup() -> None:
    prompt = _prompt()

    assert "A clean canonical Git repository is not the same" in prompt
    assert "cleanup_status=removed" in prompt
    assert "A top-level `ready` or `active` value never overrides" in prompt
    assert "does not make a hostname live" in prompt
    assert "`stubbed=false`, `live=true`" in prompt


def test_prompt_keeps_app_versioning_internal_for_nontechnical_users() -> None:
    prompt = _prompt()

    assert "APP VERSIONING IS INTERNAL — NON-OPTIONAL" in prompt
    assert "Never ask the user whether to commit" in prompt
    assert "automatically adopts, reviews, verifies, and commits unfinished app work" in prompt
    assert "An earlier build was interrupted" in prompt
