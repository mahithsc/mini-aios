# AIOS Agent Instructions

## Durable app workspaces

Application source is durable and does not belong in chat session folders. The
canonical layout is:

```text
<workspace-root>/apps/<app-id>/
├── README.md
├── .aios-app.json
├── aios.deploy.yaml
└── ...application source
```

In this development checkout, `<workspace-root>` is normally
`/Users/suneetpathangay/mini-aios/workspace`. In production it is normally
`~/.mini-aios/workspace`.

When a task names an `app_id`:

1. If the current directory already contains that app's `aios.deploy.yaml`, use
   the current directory.
2. Otherwise, look first in `<workspace-root>/apps/<app-id>`.
3. Read `README.md`, `.aios-app.json`, and `aios.deploy.yaml` before editing so
   you understand the project and its declared components.
4. Treat that app directory as the source of truth for all edits, builds,
   migrations, and deployments.

Directories under `<workspace-root>/session/<chat-id>/files` are legacy or
chat-specific scratch space. Do not create, rebuild, or redeploy an app from a
session directory when a matching durable app workspace exists. Do not invent a
replacement project in an empty session folder. If `apps/<app-id>` is missing,
report that the host must resolve or restore the app workspace instead of
silently fabricating source.

## Deployment policy

All production and preview deployments from this repository or any generated
app workspace beneath it must go through the AIOS `deploy` MCP server.

Use only these deployment tools:

- `deploy_database` for Supabase database migrations and database resources.
- `deploy_server` for Dockerized backend services on DigitalOcean.
- `deploy_frontend` for frontend applications on Vercel.

Before deploying, create a valid `aios.deploy.yaml` at the app root using the
`app_id` supplied by the main AIOS agent. Declare only the components that the
app actually contains. When an app has multiple dependent components, deploy
them in this order: database, server, frontend.

After a deploy tool returns a deployment ID, use `get_deployment_status` until
the deployment reaches a terminal state. Use `get_deployment_events` for
progress and provider-safe error information. Use `check_app_status` to inspect
all component phases plus artifact upload/verification state, and use
`get_app_info` to discover the active backend and frontend URLs. Never claim
that a deployment succeeded
or that an app is live unless the AIOS deployment status is `active` and the
URL came from `get_deployment_status` or `get_app_info`.

Do not use any built-in, bundled, or third-party deployment path. In particular:

- Do not use the Sites or `sites-hosting` skills, scripts, MCP tools, or
  `chatgpt.site` hosting.
- Do not use the Vercel CLI, Vercel MCP/app tools, Vercel skills, or Vercel API
  directly.
- Do not call Supabase, DigitalOcean, Cloudflare, or another infrastructure
  provider directly to deploy an app.
- Do not run `vercel deploy`, `wrangler deploy`, or provider-specific deployment
  commands.
- Do not create `.openai/hosting.json` or another provider-specific hosting
  configuration as a substitute for `aios.deploy.yaml`.
- Do not fall back to another deployment mechanism when an AIOS deploy tool is
  unavailable or returns an error.

If an AIOS deploy tool returns an actionable artifact, manifest, build, or
validation error, correct the local project and retry the same AIOS deploy tool.
After every material correction, the deployment must be retried; preparing a
corrected project without retrying is not completion. If the same error repeats
after a correction, or no safe local correction exists, stop and return the
exact AIOS tool error to the main agent.

If the AIOS `deploy` MCP server is unavailable, a required deployment tool is
missing, authentication fails, or the provider is unavailable, stop the
deployment workflow and return the exact AIOS tool error to the main agent.
Continue editing or testing locally only when that work remains useful, but do
not represent a local build or an external fallback deployment as a successful
AIOS deployment.

Provider credentials and user secret values must never be read, requested, or
stored in generated artifacts. Artifacts may contain only secret references and
empty environment-variable stubs; aios-cloud resolves and injects secret values.

## App media policy

Runtime images, video, and audio that must persist independently of a deployment
must use the AIOS deploy MCP media tools. Use `upload_app_media` for a file in the
current app workspace, `list_app_media` to discover stored objects,
`get_app_media_url` for a temporary private read URL, and `delete_app_media` for
removal. Do not call Supabase Storage directly and never request or use its
secret/service key. Static frontend assets that ship with a release should stay
inside the frontend artifact instead of being uploaded as runtime media.
