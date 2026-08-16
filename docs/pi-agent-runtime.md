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
extension that registers `deploy`. The extension calls a one-shot Python bridge,
which delegates to the existing deployment core and returns structured build,
health, URL, and log feedback to Pi. This preserves the build/fix/redeploy loop
without exposing another tool to the main agent.

## Safety boundary

Pi has no built-in sandbox. The validated `path` selects the starting directory
but does not confine Pi's filesystem access: its tools run with the worker
process's operating-system permissions. The launcher strips the inherited
environment to an allowlist, caps concurrent jobs and retained output, drains
stderr concurrently, uses an independent watchdog, and starts each worker in a
new process group. Production deployments that need a stronger trust boundary
must run workers as a restricted OS user or in a dedicated container with only
the target workspace mounted writable and only the selected provider credential.

## Verification

The deterministic suite uses a fake RPC subprocess to cover framing, correlated
responses, retry/settlement semantics, steering, stopping, ownership, capacity,
buffer limits, and process cleanup. Live Pi and full build/deploy tests remain
opt-in because they require provider access, network usage, and potentially
Docker.
