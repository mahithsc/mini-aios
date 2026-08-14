"""End-to-end tests for the deploy Supervisor.

HONEST by construction (see the anti-cheating contract in
docs/deploy-supervisor-PLAN.md): each test runs a REAL container executing REAL
code and makes a REAL HTTP request. The test app is a genuine minimal server, not
a mock of the Supervisor. Tests skip ONLY when Docker is genuinely unavailable —
never to dodge a real failure.
"""

from __future__ import annotations

import textwrap

import pytest

from aios_core.deploy.models import Project, Spec
from aios_core.deploy.supervisor import Supervisor, docker_available

pytestmark = pytest.mark.skipif(not docker_available(), reason="Docker not available")


def _write_min_app(dir_path, body: bytes = b"deploy-supervisor-ok", port: int = 8000):
    (dir_path / "app.py").write_text(
        textwrap.dedent(
            f"""
            from http.server import BaseHTTPRequestHandler, HTTPServer

            class H(BaseHTTPRequestHandler):
                def do_GET(self):
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write({body!r})
                def log_message(self, *a):
                    pass

            HTTPServer(("0.0.0.0", {port}), H).serve_forever()
            """
        )
    )


def test_supervisor_runs_container(tmp_path):
    """Step 1: spec -> real container serving a real HTTP response, then stop."""
    _write_min_app(tmp_path)
    project = Project(slug="e2etest1", source_dir=tmp_path, spec=Spec(run=["python", "app.py"], port=8000))
    sup = Supervisor()
    try:
        handle = sup.start(project)
        assert handle["status"] == "running"
        ok, body = sup.health(handle["url"])
        assert ok, f"app never answered; logs:\n{sup.logs(project)}"
        assert "deploy-supervisor-ok" in body  # the REAL page the container served
    finally:
        sup.stop(project)
    assert not sup.is_running(project)


def test_supervisor_restart_is_idempotent(tmp_path):
    """Starting twice replaces the container cleanly and still serves."""
    _write_min_app(tmp_path)
    project = Project(slug="e2etest2", source_dir=tmp_path, spec=Spec(run=["python", "app.py"], port=8000))
    sup = Supervisor()
    try:
        sup.start(project)
        handle = sup.start(project)  # restart
        ok, body = sup.health(handle["url"])
        assert ok, sup.logs(project)
        assert "deploy-supervisor-ok" in body
    finally:
        sup.stop(project)
    assert not sup.is_running(project)


def test_supervisor_reports_bad_run_command(tmp_path):
    """A broken run command surfaces as an error/unhealthy, not a false pass."""
    _write_min_app(tmp_path)
    project = Project(
        slug="e2etest3", source_dir=tmp_path,
        spec=Spec(run=["python", "does_not_exist.py"], port=8000),
    )
    sup = Supervisor()
    try:
        handle = sup.start(project)
        ok, _ = sup.health(handle["url"], attempts=8)
        assert ok is False  # nothing should be served
    finally:
        sup.stop(project)
