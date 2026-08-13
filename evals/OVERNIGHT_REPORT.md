# codex_subagent — overnight iteration report

Branch: `feature/codex-subagent` (off current `main`). `main` untouched, nothing pushed.

## Final case status

| Case | Tier | Score | Status | K |
|---|---|---|---|---|
| called_appropriately | behavior | 1.00 | ✅ pass | 3 |
| no_over_delegation | behavior | 1.00 | ✅ pass | 3 |
| instruction_quality (LLM judge) | behavior | 1.00 | ✅ pass | 3 |
| e2e_add_function | 1 | 1.00 | ✅ pass | 2 |
| e2e_static_site | 2 | 1.00 | ✅ pass | 2 |
| e2e_running_web_server | 2 | 1.00 | ✅ pass | 2 |
| e2e_json_api | 3 | 1.00 | ✅ pass | 2 |
| e2e_cli_with_tests | 3 | 1.00 | ✅ pass | 2 |
| e2e_refactor_passes_tests | 4 | 1.00 | ✅ pass | 2 |
| mt_multiroute_server | 5 (multi-turn) | 0.00 | ⚠️ BLOCKED | 1–2 |

Unit tests (`tests/test_codex_subagent.py`): **14 passed, 1 skipped** (live test, runs with `CODEX_LIVE_TEST=1`).

**9 of 10 case categories green at 1.00**, covering all three stated requirements:
subagent works & generates output; is called appropriately (and doesn't over-delegate);
is given correct, self-contained instructions.

## What changed (and why)

**Tool** (`aios_core/tools/`)
- `codex_subagent.py`: runs `codex exec --json`, translates its real event stream
  (agent_message / command_execution / file_change) into the shared
  `SubagentStreamEvent` protocol, streams live progress, returns Codex's final message.
- `subagent_events.py`: extracted the event protocol so `subagent` and `codex_subagent`
  share it. Registered in `tools/__init__.py`; added to `BASE_TOOLS`.

**Prompt** (`aios_core/agent_prompt.py`)
- Described `codex_subagent` as the preferred coding-delegation tool (streams progress,
  needs a self-contained task); demoted blocking `codex`; updated `writing_code` guidance.
  This is what made delegation + instruction quality reliable (behavior tier → 1.00).

**Eval harness** (`evals/codex_subagent_eval.py`)
- 6 tiered single-turn e2e cases with real verification (starts servers and curls them,
  runs tests). Difficulty tiers + global budget caps (`EVAL_MAX_E2E_RUNS`, `EVAL_MAX_MINUTES`,
  `EVAL_MAX_TIER`) so an unattended loop escalates gradually and can't run away.
- LLM human-simulator multi-turn case.
- Fixes: judge crash (literal JSON braces broke `str.format` → token replacement + tolerate
  fenced JSON); `instruction_quality` now judges only delegated runs (delegation *rate* is
  measured by `called_appropriately`); `load_dotenv()` at import so isolated tier runs get
  `OPENAI_API_KEY`.

## Budget used
~19 real Codex e2e runs (cap 60), ~6 eval invocations (cap 12), well under wall-clock cap.

## The blocked case — needs a design decision from you

**`mt_multiroute_server` (multi-turn):** across 4–5 simulated turns the main agent made
**0 delegations** to `codex_subagent`, so no artifact was produced in the eval workdir.

Root cause: the LLM human-simulator writes *natural* user messages that don't explicitly say
"use the codex subagent." Given natural, incremental requests, the agent does **not** reliably
delegate — consistent with its own prompt ("do simple edits yourself; reserve codex_subagent
for real coding"). Single-turn e2e cases pass precisely because their prompt says *"Use the
codex subagent to…"* and pass `path={workdir}`. Contributing factor: the eval agent has no
`chat_id`/workspace bound to the tmp workdir, so even self-served file writes wouldn't land
where verification looks — making non-delegated multi-turn artifacts unverifiable regardless.

This is not a tool bug. It's a question of what the multi-turn test should assert:
1. **That delegation *works* across turns (mechanism):** make the simulator reference the
   coding subagent and the working path each turn (less "natural", tests the pipe), and bind
   the eval agent's workspace to the tmp workdir.
2. **That the agent *chooses* to delegate from natural conversation (behavior):** accept that
   natural phrasing may legitimately not trigger delegation; treat delegation-rate as
   informational, or bias the agent prompt toward delegating multi-step coding.

Recommended: option 1 for a deterministic mechanism test, plus binding the agent to a
workdir-scoped workspace so artifacts are verifiable. Left for your call.

## Recommended next steps
1. Decide multi-turn intent (above) and re-enable `mt_multiroute_server` accordingly.
2. Bind the eval agent to a `chat_id`/workspace rooted at the case workdir (helps multi-turn
   and any future self-served verification).
3. Consider running the behavior + tier 1–4 suite at full K=5 once for a final signal before merge.
4. Review, then push `feature/codex-subagent` and open a PR into `main`.
