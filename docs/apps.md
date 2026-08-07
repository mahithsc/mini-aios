# Apps

An App is a durable bundle owned by mini-AIOS. Its editable source lives at
`workspace/applications/<slug>/`; its registry metadata and runtime artifacts
live under `state/`. App source is never executed directly on the host.

## Bundle layout

```text
workspace/applications/example/
├── app.json
├── skills/
│   └── guide/SKILL.md
├── mcp/
│   └── server.py
└── scripts/
    └── report.py
```

`app.json` is the only required file:

```json
{
  "schemaVersion": 1,
  "name": "Example",
  "description": "An example App",
  "version": "1.0.0",
  "skills": [
    { "id": "guide", "path": "skills/guide/SKILL.md" }
  ],
  "mcpServers": [
    {
      "id": "tools",
      "cwd": ".",
      "command": ["python", "mcp/server.py"],
      "env": {}
    }
  ],
  "executables": [
    {
      "id": "report",
      "cwd": ".",
      "command": ["python", "scripts/report.py"],
      "timeoutSeconds": 60
    }
  ],
  "prepare": [
    { "command": ["pip", "install", "-r", "requirements.txt"], "network": true }
  ],
  "runtime": {
    "network": true,
    "persistentData": false,
    "memoryMb": 512,
    "cpus": 1.0,
    "maxProcesses": 64
  }
}
```

Commands are argument arrays, not shell strings. Paths must be relative to the
App root. Network access is disabled unless both the manifest requests it and
the user approves it.

## Lifecycle

1. `create` makes an editable bundle and a registry record.
2. `validate` parses the manifest, rejects unsafe paths and file types, hashes
   the source, and creates a read-only content-addressed snapshot.
3. `prepare` installs dependencies into a versioned runtime directory using
   the generic App container image.
4. `enable` atomically activates that prepared snapshot. Skills and MCP tools
   appear to the main agent on its next turn.
5. Editing source does not change the active App. A new validate/prepare/enable
   cycle is required, so a failed update leaves the old version running.

Skill-only Apps do not require Docker. Apps containing preparation commands,
executables, or MCP servers fail closed when Docker is unavailable.

## Isolation

Containers run as a non-root user with a read-only root filesystem, dropped
capabilities, no-new-privileges, CPU/memory/process limits, bounded output, and
no Docker socket, credentials, host workspace, or state mounts. The immutable
App snapshot and prepared runtime are mounted read-only during execution.

Preparation has a monitored writable runtime mount. Executables may use
monitored persistent App data when requested. Byte and file-count ceilings are
checked before, during, and after commands. MCP servers receive persistent data
read-only; their only writable filesystem is the size-limited `/tmp`. These
portable checks reduce host-disk risk but are not a kernel filesystem quota, so
an App can briefly overshoot before Docker is stopped. Old versioned
snapshots/runtimes are garbage-collected while retaining the active, pending,
and most recent rollback versions.

Host shell, persistent process, and Codex execution paths explicitly deny App
roots. The agent can edit an App bundle with filesystem tools, but it must use
the App lifecycle to execute it.

## HTTP API

All routes require the existing local device token:

- `GET /apps`
- `POST /apps`
- `GET /apps/{id}`
- `POST /apps/{id}/validate`
- `POST /apps/{id}/prepare`
- `POST /apps/{id}/enable`
- `POST /apps/{id}/disable`
- `POST /apps/{id}/network`
- `POST /apps/{id}/executables/{executableId}/run`
- `DELETE /apps/{id}/data`
- `DELETE /apps/{id}`

Resetting data stops and disables the App first, then permanently removes its
persistent data directory. It requires explicit user approval when invoked by
the agent.

Deleting a registry entry preserves its editable source. App data remains in
state for manual recovery; it is not automatically attached to a future App
that happens to reuse the same slug.
