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
- [x] 2. **Project store + registry** — ✅ DONE — JSON-backed `ProjectStore` (durable, atomic);
      create/get/list/set_status/delete. `tests/test_deploy_store.py` (2 passed).
      (TODO later: move to the apps-infra DB registry.)
- [x] 3. **Reconciler** — ✅ DONE — `reconcile()` restarts `running` projects whose container is
      gone; validated by killing a container and confirming it re-serves.
      `test_deploy_e2e.py::test_reconciler_restarts_running`.
- [~] 4. **Public exposure** — split:
      - [x] 4a **Reverse proxy** — ✅ DONE — `proxy.py` routes `<slug>.apps.<zone>` (Host header)
            → container loopback port; register/unregister; 404 unknown, 502 dead backend.
            `test_deploy_proxy.py` (3) + `test_deploy_e2e.py::test_container_reachable_through_proxy`.
      - [~] 4b **cloudflared tunnel** — `tunnel.py` built. Quick-tunnel path (no auth) works
            (manual curl = 200), but a FRESH trycloudflare subdomain is slow/flaky to become
            DNS-resolvable (30-90s+) → opt-in live test only (`DEPLOY_TUNNEL_TEST=1`), not the
            reliable suite. Named-tunnel + stable `*.apps.trywink.io` DNS avoids this and is the
            durable path — but it is **⛔ BLOCKED**:
            ### 🔴 USER ACTION NEEDED
            The provided `CLOUDFLARE_API_TOKEN` is **invalid** (`/tokens/verify` → "Invalid API
            Token"; it's 53 chars with a `cfat_` prefix — real CF tokens are 40 chars, no prefix).
            Reliable public exposure needs a **valid CF API token** (scopes: Zone→DNS→Edit +
            Account→Cloudflare Tunnel→Edit for zone `trywink.io`) so we can create the named
            tunnel + wildcard DNS. Until then, public URL e2e stays blocked; local URLs work.
- [~] 5. **`deploy` tool Codex invokes** — feedback loop. Split:
      - [x] 5a **deploy core** — ✅ DONE — `deployer.py::deploy(slug, source_dir)`: reads
            `project.json`, build+run via Supervisor, health-check, returns structured feedback
            (url on success; error + container logs on failure so Codex can fix). 3 e2e pass.
      - [x] 5b **MCP server** — ✅ DONE — `mcp_server.py` (FastMCP) exposes `deploy(slug)`;
            validated by a REAL MCP protocol round-trip (`test_deploy_mcp.py`): stdio handshake →
            list tools → call deploy → real container deployed + served. The codex_start command
            wiring (`codex -c mcp_servers.deploy=...`) + Codex actually calling it = step 7.
      `::test_codex_build_and_deploy_loop`.
- [ ] 6. **Main-agent lifecycle tools** + prompt wiring.
- [ ] 7. **Full e2e** — `deploy_app(goal)` → Codex builds → deployed → public URL serves the
      Codex-built page. `::test_full_deploy_app_e2e`.

## E2E success criteria (the definition of done)
All tests in `tests/test_deploy_e2e.py` pass with Docker+cloudflared available (they skip only
when the runtime is genuinely absent, never to dodge a real failure). The full e2e must show a
Codex-authored app answering a real HTTP request at its public `https://<slug>.apps.trywink.io/`.

## Progress Log (append newest first)
- Step 5b DONE ✅ — `mcp_server.py` (FastMCP) exposes `deploy`; validated by a REAL MCP protocol
  round-trip (spawn over stdio as Codex does → handshake → list tools → call deploy → real
  container serves). 14 deploy tests pass. codex_start MCP wiring + Codex-calls-deploy deferred
  to step 7 (the full codex e2e). Next: step 6 (main-agent lifecycle tools: apps_list/status/
  logs/restart/stop — thin wrappers over store+supervisor, unblocked).
- Step 5a DONE ✅ — `deployer.py::deploy()`: reads project.json, build+run via Supervisor,
  health-check, returns structured feedback (url on success; error + container logs on failure).
  3 new e2e pass (serves+registers, crash surfaces logs, missing manifest errors). 13 deploy
  tests pass total. Next: 5b — expose deploy as an MCP tool to `codex exec` so Codex calls it
  in-session and iterates on the feedback. Then step 6 (main-agent lifecycle tools) + step 7
  (full deploy_app loop, LOCAL url).
- Step 4b PARTIAL + BLOCKER 🔴 — `tunnel.py` built (quick-tunnel mode). Diagnosed the live-test
  failure honestly: fresh trycloudflare subdomains are slow to resolve via DNS (`[Errno 8]
  nodename nor servname`), so quick tunnels are flaky → opt-in only. The RELIABLE path (named
  tunnel + stable `*.apps.trywink.io`) is BLOCKED on a valid Cloudflare token (provided one is
  invalid — see USER ACTION above). Pivoting the loop to the UNBLOCKED core: step 5 (deploy MCP
  tool) + step 7 full loop with LOCAL urls (public URL grafts in once a valid token arrives).
- Step 4a DONE ✅ — reverse proxy (`proxy.py`): Host-based slug routing, register/unregister,
  404/502 handling; integration e2e proves a real container is reachable THROUGH the proxy.
  10 deploy tests pass vs live Docker. Next: 4b — front the proxy with ONE cloudflared tunnel +
  DNS `*.apps.trywink.io`, wire Supervisor.start to register the route + return the public URL,
  and a real public-GET reachability test. CF token/account/zone in box .env.
- Steps 2 & 3 DONE ✅ — durable `ProjectStore` (JSON, atomic) + `reconcile()`; 6 deploy tests
  pass vs live Docker (store roundtrip/persist, supervisor, reconciler restarts a killed
  container and re-serves). Next: step 4 (public exposure — cloudflared tunnel + reverse proxy
  → https://<slug>.apps.trywink.io). CF creds are in box .env. Consider a per-app quick-tunnel
  for the first reachability test before wiring the proxy.
- Step 1 DONE ✅ — Supervisor core validated vs LIVE Docker (3 e2e passed, 8.7s): real
  container serves real HTTP, stop/restart/failure-detection honest. Docker Desktop must be
  running (`open -a Docker`; daemon booted in ~6s). Next: step 2 (Project store + registry),
  then step 3 (reconciler). Reuse apps-infra: `aios_core/apps/` (+ skill_limits,
  workspace.get_runtime_paths) — bring over without the f28e2aae checkpoint.
- (init) Plan written. Async Codex tools already built on the branch. Next: Supervisor core (#1).
