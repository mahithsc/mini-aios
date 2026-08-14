# Deploy Supervisor — build plan & loop spec

This is the north star for the autonomous build loop. Each iteration: read this,
run the e2e suite, build the next failing piece, update the Progress Log, commit.

## Goal
A **durable, one coding subagent** that builds an app with Codex and **deploys it as a
running, publicly-reachable service** — Codex authors + iterates against real deploy
feedback; a Supervisor builds/runs it in a hardened container and exposes a public URL;
running apps survive box reboot (auto-restart).

## Anti-cheating contract (do NOT violate)
The loop's success == the e2e suite passing **for real**. Cheating includes any of:
- Mocking/stubbing the component under test (the real Supervisor, real Docker, real HTTP).
- Hardcoding expected responses into the test app, or asserting on values the harness fed in.
- `skip`/`xfail` on a genuinely failing e2e to make the suite green.
- Weakening an assertion (e.g. accepting any status instead of the real page) to pass.
- Making the app trivially return the expected string without actually running the built code.
An e2e passes only when: Codex-built code runs in a real container and answers a real HTTP
request over the real (local and public) URL. If a test can't pass honestly, leave it failing
and log why in the Progress Log — do not fake it.

## Locked architecture
- **Project** (durable, cross-chat): `workspace/projects/<slug>/` + `project.json` spec
  (run cmd, port, env, prepare[], runtime{}) + registry row (DB) + runtime handle (container,
  url, health). Survives reboot.
- **Supervisor** (host service): `build` (deps in container) → `start` (run container, publish
  loopback port) → register public route → `status`/`health`/`logs` → `restart`/`stop`.
  Reconciler auto-restarts `status=running` projects on boot.
- **Public exposure**: ONE cloudflared tunnel per box + a reverse proxy that maps
  `<slug>.apps.<zone>` → the container's loopback port. Supervisor adds/removes the route on
  start/stop. Zone = `trywink.io` (Cloudflare). (Security TODO: move CF token to cloud service.)
- **One subagent**: async Codex job (`codex_start`/`codex_poll`, already built) given a
  `deploy` MCP tool that calls the Supervisor and returns real feedback (build errors, boot
  logs, health, url) so Codex iterates in-session until healthy.
- **Main-agent lifecycle tools**: `apps_list` / `app_status` / `app_logs` / `app_restart` /
  `app_stop` / `app_redeploy`.

## Reuse from `origin/codex/apps-infrastructure`
- Container hardening + manifest/validate + registry (`aios_core/apps/runtime.py`,
  `manifest.py`, `registry.py`, `service.py`, `containers/app-runtime/Dockerfile`).
- Extra deps to bring over: `aios_core/skill_limits.py`, `workspace.get_runtime_paths`/`RuntimePaths`.
- Do NOT drag in the `f28e2aae` checkpoint (gmail/doordash/mcp/execution_sandbox) — the apps
  package does not import it.

## Env / secrets (gitignored .env; rotate after)
- Box `.env`: `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_ZONE=trywink.io`.
- Cloud `.env`: AIOS_CLOUD_*, DATABASE_URL, SUPABASE_*, CLOUDFLARE_*.
- Requires Docker (Docker Desktop present on the test machine) + cloudflared.

## Build checklist (ordered; each independently testable)
- [x] 1. **Supervisor core** — spec → `docker run` a container, publish loopback port, health
      check, logs, stop/remove. ✅ DONE — `tests/test_deploy_e2e.py` 3 passed vs live Docker.
      (Still TODO at step 1 tail: graft apps-infra container hardening; currently mounts source RO.)
- [ ] 2. **Project store + registry** — durable dir + DB row; create/get/list; status.
- [ ] 3. **Reconciler** — on start, re-launch `status=running` projects.
      `::test_reconciler_restarts_running`.
- [ ] 4. **Public exposure** — cloudflared tunnel + reverse proxy; per-slug route on
      start/stop; public URL reachable. `::test_public_url_reachable`.
- [ ] 5. **`deploy` MCP tool** into `codex_start` — feedback loop.
      `::test_codex_build_and_deploy_loop`.
- [ ] 6. **Main-agent lifecycle tools** + prompt wiring.
- [ ] 7. **Full e2e** — `deploy_app(goal)` → Codex builds → deployed → public URL serves the
      Codex-built page. `::test_full_deploy_app_e2e`.

## E2E success criteria (the definition of done)
All tests in `tests/test_deploy_e2e.py` pass with Docker+cloudflared available (they skip only
when the runtime is genuinely absent, never to dodge a real failure). The full e2e must show a
Codex-authored app answering a real HTTP request at its public `https://<slug>.apps.trywink.io/`.

## Progress Log (append newest first)
- Step 1 DONE ✅ — Supervisor core validated vs LIVE Docker (3 e2e passed, 8.7s): real
  container serves real HTTP, stop/restart/failure-detection honest. Docker Desktop must be
  running (`open -a Docker`; daemon booted in ~6s). Next: step 2 (Project store + registry),
  then step 3 (reconciler). Reuse apps-infra: `aios_core/apps/` (+ skill_limits,
  workspace.get_runtime_paths) — bring over without the f28e2aae checkpoint.
- (init) Plan written. Async Codex tools already built on the branch. Next: Supervisor core (#1).
