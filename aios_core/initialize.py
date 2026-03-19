import atexit
import json
import os
from datetime import datetime

from .crons import cron_manager
from .heartbeat import heartbeat_service
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


def start_runtime(start_crons: bool = True, start_heartbeat: bool = True):
    global _RUNTIME_STARTED
    if _RUNTIME_STARTED:
        return

    os.chdir(WORKSPACE_DIR)
    initialize_files()
    if start_crons:
        cron_manager.start()
    if start_heartbeat:
        heartbeat_service.start()
    _RUNTIME_STARTED = True


def shutdown_runtime(stop_crons: bool = True, stop_heartbeat: bool = True):
    global _RUNTIME_STARTED
    if not _RUNTIME_STARTED:
        return

    if stop_crons:
        cron_manager.shutdown()
    if stop_heartbeat:
        heartbeat_service.shutdown()
    _RUNTIME_STARTED = False


def register_runtime_shutdown(stop_crons: bool = True, stop_heartbeat: bool = True):
    atexit.register(
        lambda: shutdown_runtime(
            stop_crons=stop_crons,
            stop_heartbeat=stop_heartbeat,
        )
    )
