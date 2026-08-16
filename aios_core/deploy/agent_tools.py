"""Main-agent lifecycle tools for durable deployed apps.

Deployed services outlive the coding-agent job that built them, so the main
agent needs to list / inspect / debug / restart / stop them. Thin wrappers over the
durable ProjectStore + Supervisor. ``_store``/``_sup`` are the injection seams
for tests.
"""

from __future__ import annotations

from .store import ProjectStore
from .supervisor import Supervisor


def _store() -> ProjectStore:
    return ProjectStore()


def _sup() -> Supervisor:
    return Supervisor()


def apps_list() -> dict:
    """List the apps you've deployed: slug, stored status, and whether the
    container is actually running."""
    sup = _sup()
    apps = [
        {"slug": p.slug, "status": p.status, "running": sup.is_running(p), "port": p.spec.port}
        for p in _store().list()
    ]
    return {"apps": apps}


def app_status(slug: str) -> dict:
    """Get one deployed app's status (stored status, whether it's running, port)."""
    project = _store().get(slug)
    if project is None:
        return {"error": f"unknown app: {slug}"}
    return {
        "slug": slug,
        "status": project.status,
        "running": _sup().is_running(project),
        "port": project.spec.port,
    }


def app_logs(slug: str, tail: int = 100) -> dict:
    """Fetch recent container logs for a deployed app (to debug it)."""
    project = _store().get(slug)
    if project is None:
        return {"error": f"unknown app: {slug}"}
    return {"slug": slug, "logs": _sup().logs(project, tail=int(tail))}


def app_restart(slug: str) -> dict:
    """Restart a deployed app's container."""
    store = _store()
    project = store.get(slug)
    if project is None:
        return {"error": f"unknown app: {slug}"}
    sup = _sup()
    try:
        handle = sup.start(project)
    except Exception as exc:
        store.set_status(slug, "error")
        return {"error": f"restart failed: {exc}"}
    ok, _ = sup.health(handle["url"])
    store.set_status(slug, "running" if ok else "error")
    return {"slug": slug, "status": "running" if ok else "error", "url": handle["url"], "health_ok": ok}


def app_stop(slug: str) -> dict:
    """Stop a deployed app's container (keeps its definition so it can restart)."""
    store = _store()
    project = store.get(slug)
    if project is None:
        return {"error": f"unknown app: {slug}"}
    _sup().stop(project)
    store.set_status(slug, "stopped")
    return {"slug": slug, "status": "stopped"}
