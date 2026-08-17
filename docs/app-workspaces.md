# Durable app workspaces

Application source is stored independently from chat sessions:

```text
workspace/
├── apps/
│   ├── README.md
│   └── <app-id>/
│       ├── README.md
│       ├── .aios-app.json
│       ├── aios.deploy.yaml
│       └── ...application source
└── session/
    └── <chat-id>/
        ├── chat.json
        ├── uploads/
        ├── files/       # chat scratch space only
        └── artifacts/
```

`app_create` reserves the cloud identity and creates `apps/<app-id>`. Its
returned `workspace_path` is the working directory for the Codex build.

For an existing app, `app_workspace(app_id)` resolves the durable path. During
the transition from the old layout, it scans legacy session directories,
selects the richest matching source tree, and copies it into the durable app
root. If no source exists, it reports `found=false`; callers must not fabricate
a replacement project.

Each app has non-secret `.aios-app.json` metadata recording its app ID, display
name, origin chat, and legacy source path when applicable. Existing project
READMEs are preserved; new app roots receive a short starter README.

Workspace-relative paths beginning with `apps/` resolve from the workspace
root. Parent traversal such as `../../workspace` is rejected for chat-originated
Codex jobs.
