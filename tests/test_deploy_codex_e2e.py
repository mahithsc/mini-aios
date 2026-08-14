"""Full build-and-deploy e2e (step 7): real Codex builds an app AND deploys it.

The culminating honest test: codex_start launches a real Codex session (with the
deploy MCP server wired in); Codex writes the app + project.json and calls the
`deploy` tool over MCP; the Supervisor runs it in a real container; we then fetch
the running service and assert it serves the page Codex authored.

Gated behind CODEX_DEPLOY_E2E=1 (needs Docker + Codex + network + spend) so the
fast suite stays deterministic — same pattern as the other live tests. NOT
weakened: when it runs, a Codex-built app must actually be serving over HTTP.
"""

from __future__ import annotations

import os
import time
import urllib.request

import pytest

from aios_core.deploy.models import Project, Spec
from aios_core.deploy.supervisor import Supervisor, docker_available

pytestmark = pytest.mark.skipif(
    not (docker_available() and os.getenv("CODEX_DEPLOY_E2E")),
    reason="set CODEX_DEPLOY_E2E=1 (needs Docker + Codex) for the full build-and-deploy e2e",
)

_TASK = (
    "Create two files in the CURRENT directory. "
    "app.py: a Python standard-library HTTP server that responds to GET / with exactly the "
    "bytes 'HELLO-FROM-CODEX-E2E' and listens on 0.0.0.0:8000. "
    'project.json: {"run": ["python", "app.py"], "port": 8000}. '
    "Then call the deploy tool with slug 'codexe2e' to deploy it and confirm it returns status running."
)


def test_codex_builds_and_deploys(tmp_path, monkeypatch):
    from aios_core.tools import codex_job as cj

    monkeypatch.setattr(cj, "resolve_chat_files_path", lambda p: tmp_path)

    started = cj.codex_start(task=_TASK, path=".")
    assert "job_id" in started, started
    job_id = started["job_id"]

    cursor, final = 0, None
    deadline = time.time() + 360
    while time.time() < deadline:
        r = cj.codex_poll(job_id, cursor=cursor, wait=15)
        cursor = r["cursor"]
        if r["status"] != "running":
            final = r
            break
    assert final is not None and final["status"] == "done", f"codex job did not finish: {final}"

    sup = Supervisor()
    project = Project(slug="codexe2e", source_dir=tmp_path, spec=Spec(run=["python", "app.py"], port=8000))
    try:
        assert sup.is_running(project), "Codex did not leave a running deployed container"
        url = sup.running_url(project)
        assert url, "no published URL for the deployed container"
        with urllib.request.urlopen(url, timeout=5) as resp:
            body = resp.read().decode()
        assert "HELLO-FROM-CODEX-E2E" in body  # the Codex-built app is really serving
    finally:
        sup.stop(project)
