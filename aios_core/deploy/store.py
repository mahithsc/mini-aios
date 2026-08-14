"""Durable Project store.

Persists Project records (slug, source dir, spec, status) so deployed services
survive process/box restarts. JSON-backed for v1 (atomic writes); can move to the
DB registry when the apps-infra registry is grafted in.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from .models import Project, Spec


def _default_store_path() -> Path:
    from ..workspace import get_workspace_dir

    return get_workspace_dir() / "deploy" / "projects.json"


def _to_dict(p: Project) -> dict:
    return {
        "slug": p.slug,
        "source_dir": str(p.source_dir),
        "id": p.id,
        "status": p.status,
        "spec": {
            "run": p.spec.run,
            "port": p.spec.port,
            "image": p.spec.image,
            "env": p.spec.env,
            "prepare": p.spec.prepare,
            "memory_mb": p.spec.memory_mb,
            "cpus": p.spec.cpus,
            "pids_limit": p.spec.pids_limit,
        },
    }


def _from_dict(d: dict) -> Project:
    s = d["spec"]
    return Project(
        slug=d["slug"],
        source_dir=Path(d["source_dir"]),
        id=d.get("id", ""),
        status=d.get("status", "draft"),
        spec=Spec(
            run=list(s["run"]),
            port=int(s["port"]),
            image=s.get("image", "python:3.12-slim"),
            env=dict(s.get("env", {})),
            prepare=list(s.get("prepare", [])),
            memory_mb=int(s.get("memory_mb", 512)),
            cpus=float(s.get("cpus", 1.0)),
            pids_limit=int(s.get("pids_limit", 256)),
        ),
    )


class ProjectStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else _default_store_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _read(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text())
        except Exception:
            return {}

    def _write(self, data: dict) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self.path)

    def save(self, project: Project) -> None:
        with self._lock:
            data = self._read()
            data[project.slug] = _to_dict(project)
            self._write(data)

    def get(self, slug: str) -> Project | None:
        raw = self._read().get(slug)
        return _from_dict(raw) if raw else None

    def list(self) -> list[Project]:
        return [_from_dict(v) for v in self._read().values()]

    def set_status(self, slug: str, status: str) -> None:
        with self._lock:
            data = self._read()
            if slug in data:
                data[slug]["status"] = status
                self._write(data)

    def delete(self, slug: str) -> None:
        with self._lock:
            data = self._read()
            if slug in data:
                del data[slug]
                self._write(data)
