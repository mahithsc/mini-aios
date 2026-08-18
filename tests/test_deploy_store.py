"""Project store persistence (no Docker needed)."""

from __future__ import annotations

import importlib

from aios_core.deploy.models import Project, Spec
from aios_core.deploy.store import ProjectStore


def test_project_store_roundtrip(tmp_path):
    store = ProjectStore(path=tmp_path / "projects.json")
    project = Project(
        slug="s1",
        source_dir=tmp_path / "src",
        spec=Spec(run=["python", "app.py"], port=8000, env={"FOO": "bar"}),
    )
    store.save(project)
    store.set_status("s1", "running")

    got = store.get("s1")
    assert got is not None
    assert got.slug == "s1"
    assert got.status == "running"
    assert got.spec.port == 8000
    assert got.spec.run == ["python", "app.py"]
    assert got.spec.env == {"FOO": "bar"}
    assert [p.slug for p in store.list()] == ["s1"]

    store.delete("s1")
    assert store.get("s1") is None


def test_default_store_uses_deployments_directory(tmp_path, monkeypatch):
    deployments_dir = tmp_path / ".mini-aios" / "deployments"
    workspace = importlib.import_module("aios_core.workspace")
    monkeypatch.setattr(workspace, "get_deployments_dir", lambda: deployments_dir)

    store = ProjectStore()

    assert store.path == deployments_dir / "projects.json"


def test_store_survives_new_instance(tmp_path):
    path = tmp_path / "projects.json"
    ProjectStore(path=path).save(
        Project(slug="keep", source_dir=tmp_path, spec=Spec(run=["python", "x.py"], port=9))
    )
    # A fresh store instance (simulating a restart) still sees it.
    reloaded = ProjectStore(path=path).get("keep")
    assert reloaded is not None and reloaded.slug == "keep"
