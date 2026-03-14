import atexit
import json
import os

from .crons import cron_manager
from .workspace import ensure_workspace_dir

RESET, BOLD, DIM, CYAN, GREEN, YELLOW = (
    "\033[0m", "\033[1m", "\033[2m", "\033[36m", "\033[32m", "\033[33m"
)

SKILLS_DIR = "skills"
SESSION_DIR = "session"
SESSION_MANIFEST_PATH = f"{SESSION_DIR}/session_manifest.json"
SKILLS_INDEX_PATH = f"{SKILLS_DIR}/skills_index.json"

WORKSPACE_DIR = ensure_workspace_dir()
_RUNTIME_STARTED = False


def initialize_files():
    os.makedirs(SKILLS_DIR, exist_ok=True)
    os.makedirs(SESSION_DIR, exist_ok=True)

    files_to_create = {
        SESSION_MANIFEST_PATH: [],
        SKILLS_INDEX_PATH: [],
    }

    for path, default_content in files_to_create.items():
        if not os.path.exists(path):
            with open(path, "w") as f:
                json.dump(default_content, f, indent=2)


def load_manifest():
    with open(SESSION_MANIFEST_PATH) as f:
        return json.load(f)


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
    atexit.register(lambda: shutdown_runtime(stop_crons=stop_crons))
