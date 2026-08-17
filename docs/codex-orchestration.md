# Codex child-run orchestration

Codex coding work is a durable child operation of a chat run. The Codex
app-server remains the process transport, while Mini AIOS owns lifecycle,
persistence, cancellation, and user-facing continuation.

## Lifecycle

```text
main-agent run
  -> codex_start
  -> Codex child: running
       -> awaiting_input -> running
       -> done | error | cancelled
  -> automatic main-agent continuation
  -> independent verification
  -> user-facing result
```

`codex_start` returns immediately. The main agent acknowledges that work is in
progress and ends its turn. It does not need to keep an LLM turn alive by
polling. When Codex finishes or requests input, the runtime submits a new chat
run whose trusted internal context contains the child state.

## Durable state

`CodexRunStore` persists three related records in `aios.db`:

- `codex_runs`: ownership, status, workdir, thread/turn IDs, pending input, and
  terminal result, plus process identity and recovery/verification state.
- `codex_run_events`: one ordered log for cursor-based tool activity and a
  durable gateway-event outbox.
- `codex_run_signals`: deduplicated requests for main-agent continuations,
  including the accepted continuation run ID.

The in-memory `CodexJob` object is only the live JSON-RPC transport. It is not
the lifecycle source of truth.

### Restart recovery

Each live process stores its PID and a start-time/command identity. On startup,
Mini AIOS terminates an orphan only when both still match, preventing PID-reuse
accidents. It then starts a fresh app-server transport and calls the supported
`thread/resume` protocol method with the persisted Codex thread ID. The
recovery turn first inspects the workspace, avoids repeating completed external
side effects, completes the original task, and verifies it.

Recovery is bounded by `AIOS_CODEX_MAX_RECOVERIES` (default 2). A missing thread
ID or exhausted retry budget becomes a durable error and main-agent
continuation.

An `awaiting_input` job is not restarted immediately because its old JSON-RPC
request ID belonged to the lost transport. Its structured question remains
durable. Once the user answers, a recovery turn resumes the thread with those
answers. Offline waiting jobs can also be cancelled normally.

## Continuations

The server translates a durable child signal into a normal chat run:

```text
sourceId = codex:<job-id>
turnId   = awaiting_input | done | error
```

`ChatRunner` loads the child record and injects trusted runtime context:

- `awaiting_input`: ask the exact structured questions; never invent answers.
- `done`: inspect the files and run proportionate verification before claiming
  success.
- `error`: inspect partial work when useful and report the failure safely.

Signals are claimed before submission and marked delivered afterward. Chat run
creation is independently idempotent on `(sourceId, turnId)`. If the server
crashes after creating a continuation but before acknowledging its signal, the
replayed signal resolves to the already-created run instead of producing a
duplicate.

## Events and user-visible status

Every `codex.*` event is inserted into the child event log before it is sent to
the gateway bus. Successful publication acknowledges the outbox row. Startup
replays unacknowledged rows, so transient server or bus failures do not erase
visible lifecycle progress. Each row carries a stable `codex_event_id`; the
gateway store enforces uniqueness on that ID, so a crash after publication but
before acknowledgement also replays without creating a duplicate. Diagnostic
`codex_poll` responses expose the same ordered store while filtering internal
gateway-outbox rows.

Run records expose a `display_status` suitable for UI use:

- `working` or `recovering`
- `awaiting_input`
- `verifying_changes`
- `completed`, `error`, `verification_failed`, `verification_cancelled`, or
  `cancelled`

Verification transitions also emit
`codex.verification.queued|running|completed|error|cancelled` events.

## Ordering and cancellation

Runs for the same chat are serialized with a per-chat lock. Runs for different
chats remain concurrent. Lock entries are reference-counted and removed when
the last waiting or executing turn leaves.

Interrupting a chat stops its active Codex children before cancelling the main
run. Normal server shutdown stops all attached Codex jobs. Codex processes use
their own process groups so termination reaches child commands as well as the
app-server process.

## Security boundaries

Chat-originated Codex workdirs are resolved canonically and must remain inside
the Mini AIOS workspace. Absolute paths, `..`, and symlinks cannot escape that
root.

The deployment MCP server is disabled by default. The main agent must pass
`deploy=true`, and is prompted to do so only for an explicit user deployment
request. A deploy-enabled child receives a mandatory deployment contract. Its
working directory and each MCP deploy call resolve to the nearest ancestor that
contains `aios.deploy.yaml`, so component paths such as `app/backend` still
package the complete app artifact.

Completion is fail-closed for deployment. Every component declared by the
manifest must call its corresponding AIOS MCP tool and receive a durable
`dep_...` deployment ID. If a call is missing or did not enqueue a deployment,
the host starts a bounded follow-up turn in the same Codex thread with the exact
missing postcondition. Exhausting that budget is a child error, not a false
success. Completed tool/deployment state is stored on `codex_runs` so restart
recovery does not repeat successful external side effects.

Codex can call `check_app_status(app_id)` for a single owner-scoped view of the
whole deployment pipeline. It returns normalized component phases, the latest
deployment event and error, any still-active URL, and whether the relevant
artifact was uploaded and cryptographically/archive verified by aios-cloud.

Tool-level poll, answer, and stop operations enforce the current chat's job
ownership. The HTTP routes apply the equivalent session ownership check.

## Operations

Structured lifecycle logs include job/session IDs, recovery state, terminal
status, duration, and continuation run ID. The authenticated
`GET /codex-metrics` endpoint reports status counts, live jobs, stored events,
pending gateway events, pending continuation signals, recovery count, and the
oldest active job age.

Terminal Codex records are retained for 30 days by default and swept at server
startup. Set `AIOS_CODEX_RETENTION_DAYS` to change the window; values at or
below zero disable cleanup.

The opt-in real CLI smoke test is enabled with
`AIOS_RUN_LIVE_CODEX_E2E=1`. It verifies an actual app-server edit, durable
terminal state, ownership, and continuation signal. Deterministic tests cover
restart, input recovery, outbox replay, continuation crash windows, ordering,
and containment without consuming model calls.

## Compatibility

`codex_poll` remains available for diagnostics and external clients, but normal
main-agent orchestration uses automatic continuations. The synchronous
`codex_subagent` and low-level `codex` tools remain available during migration;
new behavior evaluations target `codex_start`.
