# Durable projects

Projects are minimal, durable working directories for longer-lived work such as
websites, servers, and applications. They are independent from any one chat's
scratch space:

```text
<data-root>/
├── projects/
│   └── <project-id>/
│       └── project.md
└── sessions/
    └── <chat-id>/
        ├── scratch/
        └── uploads/
```

See [the runtime storage contract](./storage-layout.md) for development and
production data-root locations.

## Identity and lifecycle

The `projects` table in `<data-root>/state/aios.db` owns each project's stable
ID, display name, and timestamps. The directory path is derived from the ID;
there is no per-project identity JSON file and no separate project registry.

The main agent has one model-facing `project` tool. Its text `action` parameter
routes to the corresponding lifecycle operation:

- `create` requires a name and returns the new project ID and path.
- `get` returns one project by ID.
- `list` returns all projects.
- `update` changes the database-owned display name.
- `delete` removes the database row and the entire project directory.

Project deletion is destructive and should only be used when the user explicitly
asks to delete the project.

## Project contents

Mini AIOS creates only `project.md`. It does not create source, data,
attachments, worktree, or framework-specific folders. The agent chooses the
implementation structure that fits the project.

`project.md` starts by identifying itself as the project's living description
and running documentation. It is durable and agent-maintained: the agent should
read it before working and keep the project's purpose, current state, important
decisions, and useful notes there. Renaming the database record does not
overwrite this file.

## Deployment boundary

Creating a project does not deploy it and does not require a deployment
manifest. Deployment is a separate capability. Older local Supervisor and
cloud-deployment adapters may still read `deployments/projects.json`,
`.aios-app.json`, or `aios.deploy.yaml` for compatibility, but those files are
not the project registry and are not created by the `project` tool.

The first version does not automatically associate a project with a chat. The
agent receives the project path from the tool and intentionally uses that path
when it needs to work outside the current session scratch directory.
