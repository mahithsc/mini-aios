# AIOS app-domain routing implementation plan

## Current orchestration-only contract

The main agent currently exercises domain routing through deterministic stubs in
`aios_core/deploy/agent_tools.py`:

1. `create_app_artifact` accepts only a completed Codex handoff, validates
   `aios.deploy.yaml`, and registers an artifact receipt containing the
   authoritative app and component list.
2. `prepare_app_route(artifact_id)` requires that registered receipt and derives
   an opaque hostname under
   `apps.winkapiserver.org`, a routing mode, API base URL, and exact CORS origin.
3. `deploy_app_artifact(artifact_id, route_id)` rejects unissued IDs, derives the
   app and components from the artifact receipt, and requires a route receipt
   belonging to that same artifact when it has a server or frontend.
4. After all pipeline, app, component, and event status calls,
   `activate_app_route` links the route ID to the pipeline ID.
5. `app_route_status` is the final routing verification edge.

All three route tools are mocks. Their dependency receipts are process-local and
exist only to enforce the orchestration sequence; they are lost when the device
restarts. The tools perform no DNS, TLS, Cloudflare, Vercel, DigitalOcean,
durable persistence, or traffic-switching operation. Their responses use
`stubbed=true`, `live=false`, and `stubbed_not_performed` evidence fields.
They also return explicit negative evidence for artifact creation, upload,
verification, worktree cleanup, deployment, and route liveness. The chat runner
automatically appends the tool's `required_disclosure` to the saved and streamed
assistant response when the model does not reproduce it exactly.

## Stable public contract

Each app receives one opaque, immutable hostname:

```text
https://a-<opaque-key>.apps.winkapiserver.org
```

The normal full-stack routing contract is same-origin:

```text
/*       -> active frontend deployment
/api/*   -> active server deployment
```

The frontend uses `/api` as its API base. The cloud injects the exact canonical
origin into the server runtime as `AIOS_ALLOWED_ORIGINS`; neither Codex nor the
manifest supplies a production hostname. Frontend-only apps route all paths to
the frontend, while server-only apps route all paths to the server. Database-only
apps do not receive a public route.

## Production implementation locations

### 1. aios-cloud persistence and API

Implement durable route records in the `aios-cloud` repository:

- `app/db.py`: add an app-route table containing `route_id`, `app_id`, opaque
  host key, hostname, desired/active pipeline, routing mode, state, timestamps,
  and version.
- `app/models.py`: add prepare, activate, and status request/response models.
- `app/deploy_store.py`: allocate hostnames transactionally, enforce one route
  per app, resolve active provider targets from a pipeline, and compare-and-swap
  route versions during activation.
- `app/routes/deploy.py`: expose authenticated app-owned prepare, activate, and
  status endpoints. Activation accepts IDs only, never model-supplied provider
  URLs.

Host keys must be random or keyed/HMAC-derived, DNS-safe, free of user/app PII,
and collision-checked. App ownership is enforced through the existing app owner
and membership records.

### 2. Cloudflare edge provider

Add `aios-cloud/app/app_route_provider.py` and a separately deployed gateway:

- Configure proxied wildcard DNS and TLS for `*.apps.winkapiserver.org`.
- Configure a wildcard edge route to the AIOS gateway.
- Resolve the incoming hostname to an active route record.
- Proxy `/api/*` to the active DigitalOcean URL and other paths to the active
  Vercel URL; server-only and frontend-only modes use their single target.
- Return 404 for unknown, unowned, preparing, or inactive routes.
- Strip/replace forwarding headers safely, enforce request limits, and preserve
  streaming/WebSocket behavior where supported.
- Cache only route metadata and invalidate it by route version after activation.

Provider URLs remain internal routing targets. The canonical AIOS hostname is
the only user-facing application URL.

### 3. Deployment worker integration

- `aios-cloud/app/server_executor.py`: inject `AIOS_CANONICAL_ORIGIN`,
  `AIOS_ALLOWED_ORIGINS`, and the route mode as cloud-owned runtime variables.
- `aios-cloud/app/frontend_executor.py`: inject the relative `/api` base for
  full-stack apps instead of exposing the DigitalOcean URL to browser code.
- `aios-cloud/app/deploy_worker.py`: record verified provider targets but do not
  activate routing until every public component declared by the pipeline is
  healthy and active.
- Pipeline completion: atomically activate the new targets. A failed deploy must
  leave the prior route version untouched.
- Rollback: resolve targets from the selected immutable artifact/deployments and
  atomically activate a new route version without changing the hostname.

### 4. mini-aios client and main-agent tools

Replace the TODO stubs with authenticated cloud-client calls:

- `aios_core/deploy/cloud_client.py`: add prepare, activate, and route-status
  methods.
- `aios_core/deploy/agent_tools.py`: replace deterministic responses with those
  methods while preserving the current input/output contract.
- `aios_core/agent_prompt.py`: retain the enforced call order and evidence rules,
  but remove stub-only language once the endpoints are live.

### 5. Generated application convention

Codex-generated full-stack apps should:

- use relative `/api` requests from React;
- have FastAPI parse `AIOS_ALLOWED_ORIGINS` as an exact-origin list;
- never use `allow_origins=["*"]` with credentials;
- allow explicit localhost origins only in development mode; and
- avoid embedding Vercel, DigitalOcean, or AIOS production hostnames in source.

The edge gateway provides same-origin browser traffic. Exact CORS configuration
remains defense in depth and supports explicitly approved alternate origins.

## Required production tests

- Stable route allocation is idempotent and collision-safe.
- Cross-owner route access is denied.
- Database-only, server-only, frontend-only, and full-stack modes route correctly.
- Activation fails unless all declared public components are healthy and active.
- Provider URLs cannot be injected through a client request.
- Failed deployment and failed activation preserve the previous live targets.
- Rollback changes targets without changing the canonical hostname.
- Unknown hosts return 404 and never fall through to another tenant.
- Exact-origin preflight succeeds; unregistered origins receive no CORS grant.
- Route activation and status APIs are idempotent and safe under retries.
