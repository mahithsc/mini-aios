# Runtime storage layout

Mini AIOS keeps generated and user-owned data beneath one canonical data root.
The Mini AIOS service code remains in the repository or container image;
runtime data and user project source must not be written beside service source
files.

| Environment | Canonical data root |
| --- | --- |
| Development checkout | `<repository>/.mini-aios/` |
| Production process | `~/.mini-aios/` |
| Production box container | `/root/.mini-aios/` |
| Linux appliance host | `/var/lib/mini-aios/`, mounted at `/root/.mini-aios/` |

The appliance host path is an implementation detail of the container mount. To
the Mini AIOS process, the production contract is always `~/.mini-aios/`.

`AIOS_DATA_DIR` is the explicit override for tests, installers, and unusual
deployments. It points at the data root itself and takes precedence over the
development/production default. `AIOS_HOME` and `AIOS_STATE_DIR` do not select
the active data root.

## Canonical directories

```text
.mini-aios/
├── state/
│   └── aios.db
├── projects/
│   └── <project-id>/
├── sessions/
│   └── <chat-id>/
│       ├── scratch/
│       └── uploads/
├── runs/
├── skills/
├── memories/
└── deployments/
```

- `state/` contains service-owned state, including the SQLite database used
  for chats, schedules, notifications, and device state.
- `projects/` contains durable project source. A project is independent of any
  one chat even when a chat originally created it.
- `sessions/<chat-id>/scratch/` is the default working directory for a chat.
  It is suitable for intermediate files, experiments, and unpromoted work.
- `sessions/<chat-id>/uploads/` contains inbound user attachments. Stored
  attachment paths are relative to the data root, for example
  `sessions/<chat-id>/uploads/report.pdf`.
- `runs/` contains durable agent-run metadata, event logs, snapshots, and
  scheduled-run logs beneath `runs/cron_logs/`.
- `skills/` and `memories/` contain user-owned agent extensions and curated
  memory respectively.
- `deployments/` contains deployment registry and lifecycle state. Project
  source remains in `projects/`.

Uploads are owned by the session but remain outside its scratch directory, so
scratch cleanup cannot change attachment identity. Mini AIOS does not maintain
a separate user-visible artifact directory. There is no extra `workspace/`
wrapper beneath the data root.

## Agent path scopes

Agent file and shell tools default ordinary relative paths to the active chat's
scratch directory. Explicit scopes remove ambiguity:

| Agent path | Resolves beneath |
| --- | --- |
| `notes.md` | `sessions/<chat-id>/scratch/` |
| `scratch:/notes.md` | `sessions/<chat-id>/scratch/` |
| `data:/sessions/<chat-id>/uploads/report.pdf` | the current session's uploads directory |
| `data:/projects/<project-id>/...` | the canonical data root |

Existing data-root-relative paths beginning with a canonical top-level
directory remain accepted for compatibility. New prompts and tools should use
`data:/` when they intentionally leave scratch space.

## Legacy migration boundary

Older releases wrote development data to repository-level `workspace/`,
`state/`, and `memories/` directories, and used paths such as
`workspace/session/<chat-id>/files/` and `workspace/apps/<app-id>/`. These are
legacy migration inputs, not valid destinations for new writes.

Compatibility code may read or adopt those locations during migration. Legacy
top-level `uploads/<chat-id>/` content is moved to the corresponding session.
The removed top-level and per-session `artifacts/` directories are archived
under `state/migration-backups/session-layout-v2/`; they are not active runtime
storage. The resumable migration journal is
`state/migrations/session-layout-v2.json`. New records and files use the
canonical directories above, and legacy paths should remain described as
legacy wherever they appear in code, tests, or operations documentation.

An archived legacy SQLite database is retained intact after migration. Chats,
crons, cron runs, and device pairing are imported with canonical destination
data taking precedence; pairing is restored only when the canonical database
is unpaired. Legacy `gateway_events` and unrecognized tables remain archive-only
and are not imported.
