"""Reconciler: restore durable running services after a restart.

On box/process boot, any Project the store marks ``running`` but whose container
is no longer up is restarted. This is what makes deployed apps *durable* — their
uptime survives a reboot, not just their definition.
"""

from __future__ import annotations

from .store import ProjectStore
from .supervisor import Supervisor, docker_available


def reconcile(store: ProjectStore, supervisor: Supervisor | None = None) -> list[dict]:
    """Restart every project the store says should be running but isn't.

    Returns one record per acted-on project: {slug, url} on success, {slug, error}
    on failure (and the store is marked ``error`` so it isn't retried blindly)."""
    supervisor = supervisor or Supervisor()
    results: list[dict] = []
    if not docker_available():
        return results
    for project in store.list():
        if project.status != "running":
            continue
        if supervisor.is_running(project):
            continue
        try:
            handle = supervisor.start(project)
            results.append({"slug": project.slug, "url": handle["url"]})
        except Exception as exc:
            store.set_status(project.slug, "error")
            results.append({"slug": project.slug, "error": str(exc)})
    return results
