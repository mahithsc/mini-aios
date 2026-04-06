import atexit
import json
import os
from datetime import datetime

from .crons import cron_manager
from .db import initialize_app_db
from .workspace import ensure_workspace_dir

RESET, BOLD, DIM, CYAN, GREEN, YELLOW = (
    "\033[0m", "\033[1m", "\033[2m", "\033[36m", "\033[32m", "\033[33m"
)

SKILLS_DIR = "skills"
SESSION_DIR = "session"
RUNS_DIR = "runs"
APPS_DIR = "apps"
WORKSPACE_DIR = ensure_workspace_dir()
SKILLS_DIR = str(WORKSPACE_DIR / SKILLS_DIR)
SESSION_DIR = str(WORKSPACE_DIR / SESSION_DIR)
RUNS_DIR = str(WORKSPACE_DIR / RUNS_DIR)
APPS_DIR = str(WORKSPACE_DIR / APPS_DIR)
RUNS_METADATA_DIR = f"{RUNS_DIR}/metadata"
RUNS_SNAPSHOTS_DIR = f"{RUNS_DIR}/snapshots"
RUNS_EVENTS_DIR = f"{RUNS_DIR}/events"
SESSION_MANIFEST_PATH = f"{SESSION_DIR}/session_manifest.json"
SKILLS_INDEX_PATH = f"{SKILLS_DIR}/skills_index.json"
APPS_INDEX_PATH = f"{APPS_DIR}/apps.json"
_RUNTIME_STARTED = False
_SKILLS_README_PATH = f"{SKILLS_DIR}/README.md"
_SKILL_TEMPLATE_DIR = f"{SKILLS_DIR}/_template"
_SKILL_TEMPLATE_PATH = f"{_SKILL_TEMPLATE_DIR}/SKILL.md"

_SKILLS_README_CONTENT = """# Skills

Skills are reusable instructions the agent can discover and load on demand.

## Recommended structure

```text
skills/
  skills_index.json
  my-skill/
    SKILL.md
```

## How discovery works

- The agent is injected with a compact list of available skills.
- Each skill should have a `name` and `description` in YAML frontmatter.
- Full skill contents are read only when a request matches the description.

## Minimal SKILL.md

```markdown
---
name: my-skill
description: Describe what the skill does and when to use it.
---

# My Skill

## Instructions
- Put the reusable workflow here.
```

## Optional manifest

`skills_index.json` is optional. Use it when you want curated ordering, metadata
overrides, or to disable a skill without deleting it.
"""

_SKILL_TEMPLATE_CONTENT = """---
name: my-skill
description: Describe what the skill does and when to use it.
---

# My Skill

## Quick Start
- Replace this template with concise, reusable instructions.

## Workflow
1. Explain the default sequence of steps.
2. Call out important constraints or validation points.

## Additional Resources
- Link one level deep to `reference.md` or `examples.md` if needed.
"""


def initialize_files():
    os.makedirs(SKILLS_DIR, exist_ok=True)
    os.makedirs(SESSION_DIR, exist_ok=True)
    os.makedirs(APPS_DIR, exist_ok=True)
    os.makedirs(RUNS_METADATA_DIR, exist_ok=True)
    os.makedirs(RUNS_SNAPSHOTS_DIR, exist_ok=True)
    os.makedirs(RUNS_EVENTS_DIR, exist_ok=True)
    os.makedirs(_SKILL_TEMPLATE_DIR, exist_ok=True)
    initialize_app_db()

    files_to_create = {
        SESSION_MANIFEST_PATH: [],
        SKILLS_INDEX_PATH: {"version": 1, "skills": []},
        APPS_INDEX_PATH: {"version": 1, "apps": []},
    }

    for path, default_content in files_to_create.items():
        if not os.path.exists(path):
            with open(path, "w") as f:
                json.dump(default_content, f, indent=2)

    text_files_to_create = {
        _SKILLS_README_PATH: _SKILLS_README_CONTENT,
        _SKILL_TEMPLATE_PATH: _SKILL_TEMPLATE_CONTENT,
    }

    for path, content in text_files_to_create.items():
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)


def _create_manifest_timestamp() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _infer_manifest_added_at(entry: dict) -> str:
    file_name = entry.get("file")
    if isinstance(file_name, str):
        try:
            return datetime.strptime(file_name, "chat_%Y%m%d_%H%M%S.json").isoformat(
                timespec="seconds"
            )
        except ValueError:
            pass

    return _create_manifest_timestamp()


def load_manifest():
    with open(SESSION_MANIFEST_PATH) as f:
        manifest = json.load(f)

    if not isinstance(manifest, list):
        return []

    normalized_manifest = []
    manifest_changed = False

    for entry in manifest:
        if not isinstance(entry, dict):
            continue

        normalized_entry = dict(entry)
        added_at = normalized_entry.get("addedAt")
        if not isinstance(added_at, str) or not added_at:
            normalized_entry["addedAt"] = _infer_manifest_added_at(normalized_entry)
            manifest_changed = True

        normalized_manifest.append(normalized_entry)

    if manifest_changed:
        save_manifest(normalized_manifest)

    return normalized_manifest


def save_manifest(manifest):
    with open(SESSION_MANIFEST_PATH, "w") as f:
        json.dump(manifest, f, indent=2)


def start_runtime(start_crons: bool = True):
    global _RUNTIME_STARTED
    if _RUNTIME_STARTED:
        return

    os.chdir(WORKSPACE_DIR)
    initialize_files()
    if start_crons:
        cron_manager.start()
    _RUNTIME_STARTED = True


def shutdown_runtime(stop_crons: bool = True):
    global _RUNTIME_STARTED
    if not _RUNTIME_STARTED:
        return

    if stop_crons:
        cron_manager.shutdown()
    _RUNTIME_STARTED = False


def register_runtime_shutdown(stop_crons: bool = True):
    atexit.register(
        lambda: shutdown_runtime(
            stop_crons=stop_crons,
        )
    )
