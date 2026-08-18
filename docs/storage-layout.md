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
│       └── scratch/
├── uploads/
│   └── <chat-id>/
├── artifacts/
│   └── <chat-id>/
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
- `uploads/<chat-id>/` contains inbound user attachments. Stored attachment
  paths are relative to the data root, for example
  `uploads/<chat-id>/report.pdf`.
- `artifacts/<chat-id>/` contains outputs deliberately exposed to the user.
- `runs/` contains durable agent-run metadata, event logs, and snapshots.
- `skills/` and `memories/` contain user-owned agent extensions and curated
  memory respectively.
- `deployments/` contains deployment registry and lifecycle state. Project
  source remains in `projects/`.

Uploads and artifacts are deliberately not nested inside scratch space. A
scratch cleanup policy can therefore be introduced without changing attachment
identity or deleting published outputs. There is no extra `workspace/` wrapper
beneath the data root.

## Agent path scopes

Agent file and shell tools default ordinary relative paths to the active chat's
scratch directory. Explicit scopes remove ambiguity:

| Agent path | Resolves beneath |
| --- | --- |
| `notes.md` | `sessions/<chat-id>/scratch/` |
| `scratch:/notes.md` | `sessions/<chat-id>/scratch/` |
| `data:/projects/<project-id>/...` | the canonical data root |

Existing data-root-relative paths beginning with a canonical top-level
directory remain accepted for compatibility. New prompts and tools should use
`data:/` when they intentionally leave scratch space.

## Legacy migration boundary

Older releases wrote development data to repository-level `workspace/`,
`state/`, and `memories/` directories, and used paths such as
`workspace/session/<chat-id>/files/` and `workspace/apps/<app-id>/`. These are
legacy migration inputs, not valid destinations for new writes.

Compatibility code may read or adopt those locations during migration. New
records and files use the canonical directories above, and legacy paths should
remain described as legacy wherever they appear in code, tests, or operations
documentation.
