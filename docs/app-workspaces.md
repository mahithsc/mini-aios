# Durable project workspaces

Application source is stored as a durable project, independently from chat
scratch space, uploads, and artifacts:

```text
<data-root>/
├── projects/
│   ├── README.md
│   └── <app-id>/
│       ├── README.md
│       ├── .aios-app.json
│       ├── aios.deploy.yaml
│       └── ...application source
├── sessions/
│   └── <chat-id>/scratch/
├── uploads/
│   └── <chat-id>/
└── artifacts/
    └── <chat-id>/
```

See [the runtime storage contract](./storage-layout.md) for development and
production data-root locations.

`app_create` reserves the cloud identity and creates `projects/<app-id>`. Its
returned `workspace_path` is the project working directory for the Pi coding
job. The API keeps the term `workspace_path` because it describes a job's
working directory; it does not refer to a top-level `workspace/` storage
directory.

For an existing app, `app_workspace(app_id)` resolves the durable project path.
During migration it may scan legacy chat scratch directories, select the
richest matching source tree, and copy it into the canonical project root. If
no source exists, it reports `found=false`; callers must not fabricate a
replacement project.

Each project has non-secret `.aios-app.json` metadata recording its app ID,
display name, origin chat, and legacy source path when applicable. Existing
project READMEs are preserved; new project roots receive a short starter
README. This metadata remains on the device and is excluded from cloud
artifacts. The Pi deployment bridge requires its app ID to match
`aios.deploy.yaml`, and symlinked canonical project roots are rejected.

Ordinary relative agent paths resolve within the current chat's
`sessions/<chat-id>/scratch/` directory. Durable project work is entered through
the project path returned by `app_create` or `app_workspace`; parent traversal
from chat-originated Pi jobs is rejected before the worker starts.

Production deployments use `aios.deploy.yaml` and the trusted Pi cloud tools.
The legacy `project.json`/local-Docker registry at
`deployments/projects.json` remains only for compatibility with already
registered local apps and is not the authoritative deployment flow.
`legacy_apps_list` is the separate inventory for those Supervisor slugs.

## Legacy source adoption

Older releases stored app source beneath
`workspace/session/<chat-id>/files/**` or `workspace/apps/<app-id>/`. Those
paths are read only as migration inputs. Migrated chat work is recognized at
`sessions/<chat-id>/scratch/**`, and adopted app source is written to
`projects/<app-id>/`.
