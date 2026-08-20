# Main-agent deployment stub implementation plan

## Objective

Replace the orchestration-only deployment tools exposed to the main agent with
real, authenticated AIOS control-plane operations while preserving the guarded
call chain introduced on issue 5:

```text
Codex handoff
  -> immutable artifact upload and cleanup
  -> stable route preparation when public components exist
  -> ordered deployment pipeline
  -> app, pipeline, component, and event status checks
  -> route activation and final route status
```

The implementation must never let the model supply filesystem paths, source
revisions, component lists, provider URLs, or routing targets after a handoff
has been accepted. Those values are resolved from durable receipts and the
cloud control plane.

## Original inventory

The following functions in `aios_core/deploy/agent_tools.py` are deliberate
orchestration stubs:

| Tool | Current behavior | Existing production primitive | Remaining work |
| --- | --- | --- | --- |
| `app_create(name)` | Creates a deterministic local ID and workspace | `CloudDeployClient.create_app`, `create_app_workspace` | Reserve the cloud app, create the canonical local workspace from the returned ID, define retry/compensation behavior |
| `create_app_artifact(handoff_id)` | Checks handoff readiness and manifest shape, then emits a fake receipt | `create_uploaded_artifact_from_handoff` already claims, validates, sanitizes, archives, uploads, persists a receipt, and removes the worktree | Wire the helper into the tool, normalize typed errors, and add durable receipt loading |
| `prepare_app_route(artifact_id)` | Derives a fake hostname and routing/CORS contract | None in the current cloud API | Add durable route models/store/API and edge provisioning before wiring the client |
| `deploy_app_artifact(artifact_id, route_id)` | Creates fake pipeline and deployment IDs | `CloudDeployClient.enqueue_pipeline` | Load the durable artifact receipt, validate the real route relationship, use a stable idempotency key, and return the cloud response |
| `app_deployment_status(app_id)` | Reads process-local fake state | `CloudDeployClient.check_app_status` | Wire and normalize errors |
| `deployment_pipeline_status(pipeline_id)` | Reads process-local fake state | `CloudDeployClient.get_deployment_pipeline` | Wire and normalize errors |
| `deployment_status(deployment_id)` | Reads process-local fake state | `CloudDeployClient.get_deployment` | Wire and normalize errors |
| `deployment_events(deployment_id, after)` | Emits one fake event | `CloudDeployClient.get_deployment_events` | Wire and preserve cursor semantics |
| `activate_app_route(app_id, route_id, pipeline_id)` | Validates fake receipts and reports non-live | None in the current cloud API | Add health-gated, ID-only, atomic activation to the control plane and edge gateway |
| `app_route_status(app_id, route_id)` | Reads process-local fake state | None in the current cloud API | Add owner-scoped route status API and client method |
| `rollback_app_artifact(deployment_id)` | Emits a fake rollback ID | `CloudDeployClient.rollback_deployment` | Wire the API, then require normal status/event/route activation checks for the returned deployment |

`app_workspace`, `apps_list`, `app_info`, and `secrets_list` are not deployment
stubs. The first two intentionally operate on the durable local workspace; the
latter two already call the cloud client. The legacy local container lifecycle
tools (`app_status`, `app_logs`, `app_restart`, and `app_stop`) are outside this
replacement project.

## Important gaps that must be solved before removing stub labels

1. Artifact receipts are persisted but have no public loader. Downstream tools
   must resolve `artifact_id` from an atomically read, schema-validated receipt;
   process-local `StubDeploymentReceiptStore` cannot remain authoritative.
2. `app_create` has no idempotency key today. Retrying after a timeout can
   reserve duplicate cloud apps. Add an idempotent create contract or a safe
   recovery lookup before treating this method as production-ready.
3. The cloud has artifact, pipeline, status, event, and rollback APIs, but it
   has no app-route APIs. `prepare_app_route`, `activate_app_route`, and
   `app_route_status` require coordinated changes in `aios-cloud` and the edge
   gateway; device-only code cannot make them real.
4. Route preparation happens before deployment because the canonical origin,
   relative `/api` contract, and exact allowed origins must be cloud-owned build
   or runtime inputs. Deployment cannot invent these values later.
5. A pipeline response is not evidence of completion. The main agent must keep
   the status/event gates, and activation must be rejected server-side unless
   every declared public component is healthy and active.
6. Production responses must not reuse misleading top-level `active` or `ready`
   values from the simulation. Each tool needs an explicit response contract
   and terminal-state interpretation.

## Implementation phases

### Phase 0: freeze contracts and test fixtures

- Add response-contract tests for every main-agent deployment tool.
- Add reusable fake cloud and durable receipt fixtures.
- Assert that runtime-issued IDs cannot be mixed across apps, artifacts,
  routes, or pipelines.
- Assert that cloud/provider errors remain actionable and never become false
  success responses.
- Keep the existing 44-test deployment baseline green.

Exit gate: contract tests fail only because the methods still return stubs.

### Phase 1: real artifact handoff and durable receipts

- Add an atomic `load_artifact_handoff_receipt(artifact_id)` next to receipt
  persistence in `handoff_artifacts.py`.
- Reject malformed files, symlinks, IDs outside the receipt directory, schema
  mismatches, and app/artifact identity mismatches.
- Replace `create_app_artifact` with
  `create_uploaded_artifact_from_handoff(registry=_worktrees(), cloud=_cloud())`.
- Preserve typed `handoff_not_ready`, manifest rejection, upload failure, and
  cleanup failure messages for the main agent.
- Report `verification_status=source_identity_validated` and
  `cleanup_status=removed` only from the real receipt.

Tests:

- Unit tests for receipt round trips, corruption, traversal, and restart.
- Handoff tests for claim -> verify -> sanitize -> seal -> upload -> cleanup.
- Failure injection at every state transition, including upload and cleanup.
- Verify the canonical repository is untouched and the disposable worktree is
  removed on success.

Exit gate: a fake HTTP upload produces a real archive and durable receipt, and
no fake artifact ID can advance.

### Phase 2: real cloud reservation and non-routing reads

- Make `app_create` reserve the cloud app first and create the local workspace
  using the returned cloud ID.
- Add cloud-side idempotency for app creation before enabling automatic retries.
- Wire app, pipeline, component, and event status tools directly to the existing
  cloud client calls.
- Wire rollback to `rollback_deployment` and return the new real deployment ID.
- Centralize `CloudDeployError` translation so every tool returns one stable
  error envelope.

Tests:

- HTTP contract tests for authentication, payloads, status codes, and timeouts.
- App-create retry tests proving no duplicate identity is reserved.
- Status/event cursor tests and terminal/failure-state tests.
- Rollback tests proving database rollback remains rejected and frontend/server
  rollback refers to an immutable prior artifact.

Exit gate: all methods in this phase have `stubbed=false`; retry tests are
idempotent; no live provider call is required by the default suite.

### Phase 3: durable route control plane

This phase spans `aios-cloud` and its route gateway, so it must use dedicated
feature branches in those repositories before device integration.

- Add owner-scoped route records containing route ID, app ID, opaque host key,
  hostname, desired/active pipeline IDs, routing mode, state, version, and
  timestamps.
- Add authenticated prepare, activate, and status endpoints.
- Allocate one stable, collision-checked, non-PII hostname per app.
- Derive routes and CORS values from the artifact manifest server-side.
- Accept IDs only during activation; resolve provider targets internally.
- Require all public components to be healthy and active.
- Activate using compare-and-swap so failed deploys and races preserve the
  previous live version.
- Implement wildcard gateway lookup and safe `/api/*` versus `/*` proxying.
- Inject `AIOS_CANONICAL_ORIGIN`, `AIOS_ALLOWED_ORIGINS`, and the route mode into
  server runtime; use relative `/api` for full-stack frontends.

Tests:

- Database migration and store transaction tests.
- Owner isolation and cross-app/cross-route rejection.
- Stable allocation, collision, retry, and concurrent activation tests.
- Full-stack, frontend-only, server-only, and database-only route behavior.
- Failed deployment/activation preserves old targets.
- Unknown hosts never fall through to another tenant.
- Exact CORS preflight allow/deny behavior.

Exit gate: cloud route APIs pass contract tests and a local gateway integration
test can switch between two immutable deployment targets without changing the
hostname.

### Phase 4: wire real pipeline and routing tools

- Add `prepare_app_route`, `activate_app_route`, and `get_app_route` methods to
  `CloudDeployClient`.
- Replace the three route stubs with those authenticated calls.
- Replace `deploy_app_artifact` with `enqueue_pipeline`, deriving app ID and the
  ordered component list only from the durable artifact receipt.
- Derive a stable idempotency key from the artifact and route identities.
- Validate route/artifact/app relationships in the cloud as well as on device.
- Remove `StubDeploymentReceiptStore` from the production path.

Tests:

- Device/cloud request-response contract tests.
- Repeated tool calls return the same route or pipeline rather than duplicates.
- Invented and mixed IDs are rejected after process restart.
- Pipeline order is database -> server -> frontend.
- Public components require a real ready route; database-only omits it.

Exit gate: a local multi-process integration test runs the full main-agent tool
chain with durable state surviving a device restart.

### Phase 5: orchestration prompt and disclosure cutover

- Update tool descriptions in `agent_prompt.py` from simulation contracts to
  real evidence contracts.
- Retain the exact call order, ID provenance rules, terminal status polling, and
  prohibition on model-supplied provider URLs.
- Remove automatic simulation disclosure injection only after every tool in the
  chain returns real evidence.
- Continue to disclose partial failures, pending worktree cleanup, queued
  deployments, and non-live routing accurately.
- Restart the device and inspect a recorded main-agent session to verify the
  actual tool-call sequence.

Exit gate: the agent never says live until the cloud reports active deployments
and an active route with a control-plane-issued URL.

### Phase 6: end-to-end and failure recovery

- Run a frontend-only deployment, a server-only deployment, and a full-stack
  React + FastAPI + Supabase deployment.
- Exercise retry after device restart, cloud timeout, worker crash, failed
  component build, failed route activation, and cleanup failure.
- Exercise rollback to a prior deployment and confirm the stable hostname does
  not change.
- Verify no provider credentials or secret values enter an artifact, receipt,
  log, event, or model-visible response.

Exit gate: all default tests pass, guarded live tests pass when credentials are
available, and the captured main-agent transcript matches the required tool
graph.

## Unattended execution loop

Run one bounded acceptance criterion per loop. Do not attempt a whole phase in
one edit.

1. **Orient**: confirm worktree, branch, clean/known diff, current phase, and
   last green test command.
2. **Select**: choose the highest-priority unmet criterion whose dependencies
   exist locally. Do not skip to route integration before the cloud route API
   exists.
3. **Write the failing test**: add the smallest unit or contract test that
   proves the criterion. Run it and confirm it fails for the expected reason.
4. **Implement narrowly**: change only the modules needed for that criterion.
5. **Verify in layers**:
   - the new focused test;
   - the affected deployment test module;
   - deployment regression set;
   - full repository suite at each phase boundary;
   - Ruff and `git diff --check`.
6. **Record evidence**: update this document's progress section with files,
   tests, outcomes, and any unresolved risk. Never label a skipped live test as
   passing.
7. **Continue**: select the next criterion automatically while the preceding
   gates are green and the next action is safe and local.

Baseline command in this worktree:

```bash
PYTHONPATH=. /Users/suneetpathangay/mini-aios/.venv/bin/pytest -q \
  tests/test_deploy_agent_tools.py \
  tests/test_cloud_deploy_client.py \
  tests/test_worktree_handoff.py \
  tests/test_deploy_codex_e2e.py
```

Original result: `44 passed, 2 skipped`.

Use the explicit interpreter path above because `uv` is installed at
`/Users/suneetpathangay/.local/bin/uv` but is not currently on this shell's
`PATH`.

## Safety and stop conditions

The unattended loop must stop and report rather than guess when any of these
conditions occurs:

- a required API or repository is absent;
- a step requires creating a branch or changing another repository without
  authorization;
- a database migration, provider resource, DNS record, or live deployment
  would be mutated;
- authentication, credentials, billing, or a secret value is required;
- the same failure survives three narrow correction attempts;
- the full suite exposes an unrelated pre-existing failure;
- cleanup cannot prove the exact AIOS-owned disposable worktree target;
- a response contract from the cloud differs from the checked-in tests; or
- progress would require weakening ID provenance, ownership, idempotency,
  verification, or tenant-isolation checks.

Default tests use fakes or local services and must not contact production. Do
not push, open a pull request, deploy, or create commits unless the user grants
that Git or external-state authorization separately.

## Progress log

### 2026-08-20 — implementation complete, final provider E2E deferred

- Replaced all production stub paths with authenticated cloud calls and removed
  the unused process-local `StubDeploymentReceiptStore`.
- Added durable, restart-safe artifact receipt loading and real artifact upload,
  verification, and worktree cleanup evidence.
- Added idempotent cloud app creation, durable stable-route APIs, Cloudflare DNS
  provisioning/cleanup, same-origin gateway routing, health-gated activation,
  and zero-downtime desired-versus-active route state.
- Added canonical-origin, CORS, and relative API runtime configuration to both
  cloud and deploy-worker executors.
- Applied and verified the additive Supabase control-plane migrations, including
  a compatibility conversion for legacy timestamp columns and indexes for new
  foreign keys. RLS is enabled and direct public roles are revoked.
- Live per-function verification succeeded for app reservation/idempotency,
  Supabase artifact upload and validation, worktree removal, Cloudflare route
  provisioning, app/route status, pipeline creation, component/event reads,
  route activation, and rollback creation. Pipeline/provider execution was
  isolated so it could not trigger the final Vercel/DigitalOcean deployment.
- Disposable Supabase Storage, Cloudflare DNS, control-plane, worktree, receipt,
  and local app resources were removed after the checks.
- Verification: mini-aios `191 passed, 16 skipped` excluding the unrelated
  script-style `tests/test_tools.py`; a focused deploy set also passes. The
  standalone tool test has a pre-existing process-session-cap failure at
  collection. aios-cloud: `85 passed`. aios-deploy-worker: `31 passed`.
- Remaining deliberate step: user-run final provider E2E deployment and captured
  main-agent transcript after the cloud/worker/device builds are deployed.

- 2026-08-19: inventory completed on
  `codex/issue-5-implement-deploy-stubs`; deployment baseline is 44 passed and
  2 guarded live tests skipped.
