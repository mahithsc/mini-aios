import atexit
import json
import os

from .crons import cron_manager
from .db import initialize_app_db
from .storage_migration import migrate_legacy_storage
from .workspace import ensure_runtime_dirs, get_runtime_paths, get_state_dir, get_skills_dir

RESET, BOLD, DIM, CYAN, GREEN, YELLOW = (
    "\033[0m", "\033[1m", "\033[2m", "\033[36m", "\033[32m", "\033[33m"
)

_PATHS = get_runtime_paths()
WORKSPACE_DIR = str(_PATHS.workspace)
SKILLS_DIR = str(_PATHS.skills)
RUNS_DIR = str(_PATHS.runs)
RUNS_METADATA_DIR = str(_PATHS.runs / "metadata")
RUNS_SNAPSHOTS_DIR = str(_PATHS.runs / "snapshots")
RUNS_EVENTS_DIR = str(_PATHS.runs / "events")
SKILLS_INDEX_PATH = str(_PATHS.skills / "skills_index.json")
_RUNTIME_STARTED = False
_SKILLS_README_PATH = str(_PATHS.skills / "README.md")
_SKILL_TEMPLATE_DIR = str(_PATHS.skills / "_template")
_SKILL_TEMPLATE_PATH = str(_PATHS.skills / "_template" / "SKILL.md")

_SKILLS_README_CONTENT = """# Skills

Skills are reusable instructions the agent can discover and load on demand.

## Recommended structure

```text
skills/
  skills_index.json
  my-skill/
    SKILL.md
```

Skills live outside the user workspace and are read-only to agents.

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


def _initialize_skill_files() -> None:
    skills_dir = get_skills_dir()
    template_dir = skills_dir / "_template"
    template_dir.mkdir(parents=True, exist_ok=True)

    index_path = skills_dir / "skills_index.json"
    if not index_path.exists():
        index_path.write_text(
            json.dumps({"version": 1, "skills": []}, indent=2),
            encoding="utf-8",
        )

    readme_path = skills_dir / "README.md"
    if not readme_path.exists():
        readme_path.write_text(_SKILLS_README_CONTENT, encoding="utf-8")

    template_path = template_dir / "SKILL.md"
    if not template_path.exists():
        template_path.write_text(_SKILL_TEMPLATE_CONTENT, encoding="utf-8")


def initialize_files() -> None:
    # State and the skills root must exist before importing legacy data.
    # Default skill files are created only after migration so they cannot
    # displace the active legacy manifest.
    # The workspace's three user-facing directories are created by the
    # migration only after old lowercase/internal entries have been staged.
    get_state_dir().mkdir(parents=True, exist_ok=True)
    get_skills_dir().mkdir(parents=True, exist_ok=True)
    initialize_app_db()

    from .sessions import initialize_chat_storage

    initialize_chat_storage()
    migrate_legacy_storage()
    _initialize_skill_files()
    ensure_runtime_dirs()


def start_runtime(start_crons: bool = True):
    global _RUNTIME_STARTED
    if _RUNTIME_STARTED:
        return

    initialize_files()
    os.chdir(get_runtime_paths().applications)
    if start_crons:
        cron_manager.start()
    _RUNTIME_STARTED = True


def shutdown_runtime(stop_crons: bool = True):
    global _RUNTIME_STARTED

    try:
        from .tools.processes import close_all_processes

        close_all_processes()
    except Exception:
        pass

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
