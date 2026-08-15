# DoorDash MCP integration

DoorDash follows the same local integration shape as Gmail:

```text
server/integrations/doordash.py
        │ connection status and login route
        ▼
aios_core/integrations/doordash.py
        │ dd-cli login + state (no credentials)
        ▼
aios_core/integrations/doordash_mcp.py
        │ local stdio MCP registration
        ▼
aios_core/mcp_servers/doordash/
        ├── manifest.json
        ├── server.py
        └── client.py ──► dd-cli ──► DoorDash
```

The bundled usage policy lives in `skills/doordash-cli/SKILL.md`.

## Install and connect

Install the DoorDash CLI for the mini-AIOS host architecture and make `dd-cli`
executable on `PATH`. If it lives elsewhere, set:

```bash
AIOS_DOORDASH_CLI_PATH=/absolute/path/to/dd-cli
```

As soon as mini-AIOS can resolve the executable, it registers the local
DoorDash MCP tool. It does not require a duplicate mini-AIOS `connected` record:
`dd-cli` and its operating-system keychain are the authority on whether a
command is authenticated.

When `dd-cli` reports that login is required, call
`POST /integrations/doordash/connect` using the local device token. The request
runs `dd-cli login`, which opens the provider's browser flow and stores the
resulting credential in the operating-system keychain. The request completes
after login succeeds.

`GET /integrations/doordash` returns installation, tool availability, and the
last mini-AIOS-observed login status. `DELETE /integrations/doordash` clears
that observed status, but it does not unregister the installed tool or claim to
remove the keychain credential because the current CLI has no logout command.

## Credential boundary

mini-AIOS never accepts, stores, or forwards the DoorDash token. The CLI owns
the token and reads it from the operating-system keychain for each command.
mini-AIOS stores only non-secret connection metadata in SQLite.

The MCP subprocess receives only the absolute CLI path. It accepts DoorDash CLI
arguments, parses them into an argument array, and invokes only the configured
`dd-cli` executable—never a shell or an arbitrary executable.

## MCP surface

The server exposes one tool, `run_cli(arguments)`. `arguments` is the text that
would follow `dd-cli`. The adapter uses shell-style quoting only to split that
text into an argument array; it never launches a shell. It injects
`--json-output`, rejects `--beautify`, and keeps `login` on the separate
connection route.

This keeps the command grammar in one place: the installed CLI. The companion
skill describes the supported workflows and teaches the agent which argument
strings to build.

## Safety boundary

Because a generic CLI command can be read-only or financial, the MCP tool is
conservatively annotated as destructive and non-idempotent. DoorDash's
`order submit` command still requires the CLI's non-interactive `--yes` flag.
The skill allows that flag only after a fresh preview and explicit approval of
the total, tip, fulfillment details, payment card, and CLI submission.
Submission must never be retried automatically, and it is not considered
successful until `order status` returns `successful`.

## Current packaging limitation

The MCP server and skill are part of the mini-AIOS Python tree for this first
iteration. The existing macOS arm64 `dd-cli` binary cannot execute inside a
Linux container. A containerized app will therefore need a DoorDash CLI build
for its target architecture, or it must call a host-side broker. That packaging
decision is intentionally left for the cohesive app format.
