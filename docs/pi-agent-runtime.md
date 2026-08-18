# Pi coding-agent runtime

Mini AIOS exposes Pi to the main agent through one public tool:

```text
pi(action="start" | "poll" | "steer" | "stop" | "list", ...)
```

The public action dispatcher owns a background job manager. Each job launches a
pinned Pi CLI in RPC mode, exchanges LF-delimited JSON over stdin/stdout, and
normalizes Pi events before returning them to the main agent or gateway.

## Lifecycle

- `start` validates the task and working directory, reserves a job slot, starts
  `pi --mode rpc --no-session`, performs a `get_state` readiness handshake, and
  sends the initial `prompt` command.
- `poll` returns buffered activity after an absolute cursor and optionally waits
  briefly for progress.
- `steer` sends an RPC `steer` command to a running job.
- `stop` sends RPC `abort`, then terminates the complete process group.
- `list` returns jobs owned by the current chat.

An accepted prompt is not complete. A job becomes `done` only after Pi emits
`agent_settled`; `agent_end` may still be followed by retry or compaction.

## Pi launch policy

Resource discovery is disabled for unattended workers. The launcher uses
`--no-approve`, disables discovered extensions, skills, prompts, and themes,
and explicitly loads only repository-owned extensions. Coding jobs receive the
Pi built-ins `read,bash,edit,write,grep,find,ls`; read-only jobs receive
`read,grep,find,ls`.

`start` may select a provider, model, and thinking level, but it cannot supply an
API key. Credentials come only from Pi's configured home or the launcher's
filtered server environment. Pi is version-pinned in the production image
because the RPC protocol has no version-negotiation handshake.

## Deployment

Pi intentionally has no MCP client. Mini AIOS therefore supplies a trusted Pi
extension backed by a finite one-shot Python bridge. The bridge exposes:

- `deploy` plus durable pipeline and component status/lifecycle tools;
- cloud app information and status;
- workspace-confined app media upload/list/URL/delete operations; and
- structured, policy-checked database table inspection and read-only queries.

`deploy` is locked to Pi's current manifest-rooted workspace, validates and
uploads one artifact, and asks aios-cloud to orchestrate database, server, and
frontend tiers in dependency order. Its Pi tool-call-derived operation ID makes
one invocation idempotent; the cloud client rejects pipeline calls without a
stable operation key. Durable workspace metadata must match the manifest and is
never included in the artifact. Dependency/cache trees, credential files, local
database files, and high-confidence embedded credentials are also excluded or
rejected before upload. Pipeline status prevents Pi from mistaking an
accepted request for a live deployment. The database bridge accepts structured
filters and ordering, never raw SQL. Provider credentials, user secret values,
generic HTTP access, arbitrary source roots, and direct provider deployment
tools are not exposed through the extension.

## Safety boundary

Pi has no built-in sandbox. The validated `path` selects the starting directory
but does not confine Pi's filesystem access: its tools run with the worker
process's operating-system permissions. The launcher strips the inherited
environment to an allowlist, caps concurrent jobs and retained output, drains
stderr concurrently, uses an independent watchdog, and starts each worker in a
new process group. Production deployments that need a stronger trust boundary
must run workers as a restricted OS user or in a dedicated container with only
the target workspace mounted writable. The filtered worker environment contains
model-provider authentication and the device-scoped aios-cloud credential needed
by the trusted bridge; because Pi also has a shell, these credentials are inside
the same OS trust boundary even though application and infrastructure-provider
secrets remain excluded.

## Verification

The deterministic suite uses a fake RPC subprocess to cover framing, correlated
responses, retry/settlement semantics, steering, stopping, ownership, capacity,
buffer limits, bridge validation, trusted-extension loading, and process cleanup.
Live Pi and full cloud deployment tests remain opt-in because they require
provider access, network usage, model spend, and cloud resources. Legacy local
Supervisor tests additionally require Docker.
