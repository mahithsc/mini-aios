# codex_subagent — overnight iteration report (honest final)

Branch: `feature/codex-subagent` (off current `main`). `main` untouched, nothing pushed.

## TL;DR
- **The tool is solid and merge-ready.** `codex_subagent` works end-to-end — unit tests
  green and real Codex builds artifacts through it (verified live).
- **The three stated requirements are robustly met** (behavior evals, K=3, all 1.00):
  called-appropriately, no-over-delegation, correct-instructions.
- **The e2e / multi-turn eval harness is NOT yet a reliable gate.** Individual cases pass in
  isolation, but a consolidated/batch run at K=1 is flaky. Do not treat the e2e tier as
  "green" until it's hardened (see below). This is an honest limitation, not a tool defect.

## What is solid

| Layer | Status | Evidence |
|---|---|---|
| Unit tests (`tests/test_codex_subagent.py`) | ✅ robust | 14 passed, 1 skipped (mocked against real captured Codex JSONL) |
| Live end-to-end tool | ✅ verified | `CODEX_LIVE_TEST=1` — real Codex creates a file through the tool |
| Behavior: called_appropriately | ✅ 1.00 (K=3) | agent reliably delegates coding tasks to codex_subagent |
| Behavior: no_over_delegation | ✅ 1.00 (K=3) | agent doesn't delegate trivial work |
| Behavior: instruction_quality | ✅ 1.00 (K=3) | LLM judge: delegated tasks are self-contained/actionable |

The tool implementation (`aios_core/tools/codex_subagent.py`, `subagent_events.py`) and the
prompt steering (`aios_core/agent_prompt.py`) are done and I'd merge them.

## What is NOT reliable yet: the e2e / multi-turn eval harness

Single-turn e2e cases (add_function, static_site, running_web_server, json_api,
cli_with_tests, refactor) each **passed when run one-at-a-time** (fresh process, K=2), which is
why an earlier pass looked "10/10 green." But that was fragile. A **consolidated run** (all
cases, one process, K=1) exposed real problems:

- **Shared-workspace pollution / no delegation:** with no per-case isolation, the agent
  inspected the shared `workspace/` dir, saw stale artifacts from prior cases (`calc.py`,
  `index.html`, `server.py`…), concluded the task was done, and didn't delegate — leaving the
  fresh temp workdir empty. Partially addressed by adding `_chat_files_dir(workdir)` isolation
  to `eval_e2e_case` (mirroring the multi-turn fix), but this alone did **not** make batch runs
  reliable.
- **K=1 non-determinism:** at one shot per case, normal Codex variance flips results — some
  runs don't produce the artifact, some produce a server that crashes on start (`rc=1`). The
  ≥80% threshold is only meaningful at K≥3.
- **Verify strictness:** a couple of checks (e.g. server must start and serve a route) are
  brittle to benign variation.

Net: the e2e/multi-turn tier measures something real (Codex genuinely builds working sites and
servers — observed repeatedly), but as currently written it is **not a trustworthy pass/fail
gate**. `cli_with_tests` and `refactor` were the most stable; server-based cases the least.

Multi-turn (`mt_multiroute_server`): the isolation fix made the agent delegate across turns
(good), but it's flaky at the route level (`/about` 404'd in one run). Functional, not reliable.

## Changes committed (branch `feature/codex-subagent`)
- Tool + event protocol + registration + prompt steering (`1dc891a`, `5097945`).
- Eval harness: tiered e2e cases, LLM multi-turn simulator, budget caps, judge fix, metric fix,
  `.env` load, multi-turn isolation + redesign (`bb949c5`, `2a284eb`, `5ce2c36`, `6d6edf4`).
- This commit: `_chat_files_dir` isolation for single-turn e2e (necessary but insufficient) +
  this honest report.

## Budget consumed
Roughly 40+ real Codex e2e runs across the night (individual tier passes + two consolidated
diagnostic runs). Wall-clock: several hours. Unit tests stayed green throughout.

## Recommended next steps (harden the e2e gate before trusting it)
1. Run e2e/multi-turn at **K≥3** so the ≥80% threshold is meaningful against Codex variance.
2. Make isolation hermetic: give the eval agent a `chat_id`/workspace rooted at the case
   workdir (the `_chat_files_dir` shim is a start) and clean/point the shared `workspace/` per
   run so the agent never sees cross-case artifacts.
3. Nudge the agent to delegate with the correct absolute path (or rely fully on the isolated
   workdir) so artifacts always land where verify looks.
4. Loosen brittle verifies (accept a served route on any 2xx; retry server startup).
5. Re-run a single consolidated pass and treat *that* as the authoritative scorecard.

## Bottom line for merge
Merge the **tool + prompt + unit tests + behavior evals** — those are done and trustworthy.
Treat the **e2e/multi-turn eval suite as a work-in-progress** that needs the hardening above
before it should gate anything.
