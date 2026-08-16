"""Opt-in full loop: real Pi authors an app and deploys it through its extension."""

from __future__ import annotations

import os
import shutil
import time
import urllib.request

import pytest

from aios_core.deploy.models import Project, Spec
from aios_core.deploy.supervisor import Supervisor, docker_available


pytestmark = pytest.mark.skipif(
    not (docker_available() and shutil.which("pi") and os.getenv("PI_DEPLOY_E2E")),
    reason="set PI_DEPLOY_E2E=1 (needs Docker, Pi auth, network, and model spend)",
)

_SLUG = "pie2e"
_MARKER = "HELLO-FROM-PI-E2E"
_TASK = (
    "Create two files in the current directory. "
    "app.py must use only Python's standard library, listen on 0.0.0.0:8000, "
    f"and respond to GET / with exactly {_MARKER!r}. "
    'project.json must be exactly compatible with '
    '{"run": ["python", "app.py"], "port": 8000}. '
    f"Then call the deploy tool with slug {_SLUG!r}. If deployment returns an "
    "error, inspect its logs, fix the files, and retry until it reports status running."
)


def test_pi_builds_and_deploys(tmp_path, monkeypatch):
    from aios_core.tools.pi import pi
    from aios_core.tools.pi_job import close_all_pi_jobs

    monkeypatch.setenv("AIOS_PI_ALLOWED_ROOTS", str(tmp_path))
    started = pi(action="start", task=_TASK, path=str(tmp_path))
    assert "job_id" in started, started
    job_id = started["job_id"]

    cursor, final = 0, None
    deadline = time.time() + 360
    while time.time() < deadline:
        response = pi(action="poll", job_id=job_id, cursor=cursor, wait=15)
        cursor = response["cursor"]
        if response["status"] not in {"starting", "running", "stopping"}:
            final = response
            break

    sup = Supervisor()
    project = Project(
        slug=_SLUG,
        source_dir=tmp_path,
        spec=Spec(run=["python", "app.py"], port=8000),
    )
    try:
        assert final is not None and final["status"] == "done", final
        assert sup.is_running(project), "Pi did not leave a deployed container running"
        url = sup.running_url(project)
        assert url, "deployed container has no published URL"
        with urllib.request.urlopen(url, timeout=5) as response:
            body = response.read().decode()
        assert _MARKER in body
    finally:
        sup.stop(project)
        close_all_pi_jobs()
