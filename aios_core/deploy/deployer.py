"""Deployment core used by coding-agent and main-agent adapters.

Given a project directory that a coding agent authored (code + ``project.json``), build and
run it as a service, health-check it, and return STRUCTURED FEEDBACK the caller can
act on: on success the URL; on failure the error + container logs so an agent can fix
and re-deploy. It never raises for expected failures — it returns ``status: error``
so the feedback loop keeps going.

``project.json`` (the coding agent writes it):
    {"run": ["python", "app.py"], "port": 8000,
     "image": "python:3.12-slim", "env": {}, "prepare": []}
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

from .models import Project, Spec
from .store import ProjectStore
from .supervisor import Supervisor, docker_available, project_has_deps


class DeployError(Exception):
    pass


def load_spec(source_dir: Path) -> Spec:
    manifest = source_dir / "project.json"
    if not manifest.exists():
        raise DeployError("project.json not found in the project directory")
    try:
        data = json.loads(manifest.read_text())
    except Exception as exc:
        raise DeployError(f"project.json is not valid JSON: {exc}")
    if "run" not in data or "port" not in data:
        raise DeployError("project.json must include 'run' (array) and 'port' (int)")
    return Spec(
        run=list(data["run"]),
        port=int(data["port"]),
        image=data.get("image", "python:3.12-slim"),
        env=dict(data.get("env", {})),
        prepare=[list(c) for c in data.get("prepare", [])],
        memory_mb=int(data.get("memory_mb", 512)),
        cpus=float(data.get("cpus", 1.0)),
        pids_limit=int(data.get("pids_limit", 256)),
    )


def deploy(
    slug: str,
    source_dir,
    store: ProjectStore | None = None,
    supervisor: Supervisor | None = None,
) -> dict:
    """Build + run the project as a service and return structured feedback.

    Returns one of:
      {status: running, url, host_port, health_ok, response_sample}
      {status: error, error, [url], [logs]}   ← caller reads logs to fix and retry
    """
    source_dir = Path(source_dir)
    supervisor = supervisor or Supervisor()
    if not docker_available():
        return {"status": "error", "error": "docker is not available on this host"}
    try:
        spec = load_spec(source_dir)
    except DeployError as exc:
        return {"status": "error", "error": str(exc)}

    project = Project(slug=slug, source_dir=source_dir, spec=spec, status="deploying")
    if store is not None:
        store.save(project)
    try:
        handle = supervisor.start(project)
    except Exception as exc:
        if store is not None:
            store.set_status(slug, "error")
        return {"status": "error", "error": f"failed to start container: {exc}"}

    # Installing dependencies (pip) can take a while; be patient before calling it unhealthy.
    if project_has_deps(project):
        ok, body = supervisor.health(handle["url"], attempts=180, delay=0.5)  # ~90s
    else:
        ok, body = supervisor.health(handle["url"])
    if not ok:
        logs = supervisor.logs(project)
        if store is not None:
            store.set_status(slug, "error")
        return {
            "status": "error",
            "url": handle["url"],
            "error": "the app did not become healthy (did it bind the declared port?)",
            "logs": logs[-2000:],
        }

    if store is not None:
        store.set_status(slug, "running")
    return {
        "status": "running",
        "url": handle["url"],
        "host_port": handle["host_port"],
        "health_ok": True,
        "response_sample": body[:200],
    }
