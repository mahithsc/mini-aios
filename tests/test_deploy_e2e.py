"""End-to-end tests for the deploy Supervisor.

HONEST by construction (see the anti-cheating contract in
the deployment supervisor): each test runs a REAL container executing REAL
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


def test_reconciler_restarts_running(tmp_path):
    """Step 3: after a simulated reboot (container gone, store says running), the
    reconciler brings the real service back up and it serves again."""
    from aios_core.deploy.reconciler import reconcile
    from aios_core.deploy.store import ProjectStore

    _write_min_app(tmp_path)
    store = ProjectStore(path=tmp_path / "projects.json")
    project = Project(slug="recon1", source_dir=tmp_path, spec=Spec(run=["python", "app.py"], port=8000))
    sup = Supervisor()
    try:
        sup.start(project)
        store.save(project)
        store.set_status("recon1", "running")

        # simulate a reboot: the container is gone but the store still says running
        sup.stop(project)
        assert not sup.is_running(project)

        result = reconcile(store, sup)
        record = next((r for r in result if r["slug"] == "recon1"), None)
        assert record is not None and "url" in record, f"reconcile did not restart: {result}"
        assert sup.is_running(project)
        ok, body = sup.health(record["url"])
        assert ok and "deploy-supervisor-ok" in body  # really serving again
    finally:
        sup.stop(project)


def test_container_reachable_through_proxy(tmp_path):
    """Step 4a integration: a real container is reachable THROUGH the reverse
    proxy via its slug Host header (the full local public-exposure path)."""
    import urllib.request

    from aios_core.deploy.proxy import ReverseProxy

    _write_min_app(tmp_path)
    project = Project(slug="proxied1", source_dir=tmp_path, spec=Spec(run=["python", "app.py"], port=8000))
    sup = Supervisor()
    proxy = ReverseProxy(apps_domain="apps.trywink.io")
    proxy.start()
    try:
        handle = sup.start(project)
        ok, _ = sup.health(handle["url"])
        assert ok, sup.logs(project)

        public_url = proxy.register(project.slug, handle["host_port"])
        assert public_url == "https://proxied1.apps.trywink.io/"

        req = urllib.request.Request(
            f"http://127.0.0.1:{proxy.port}/", headers={"Host": "proxied1.apps.trywink.io"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode()
        assert "deploy-supervisor-ok" in body  # container served through the proxy
    finally:
        proxy.unregister(project.slug)
        proxy.shutdown()
        sup.stop(project)


def test_deploy_project_serves_and_registers(tmp_path):
    """Step 5a: deploy() builds+runs a project dir, it really serves, and the store
    records it as running."""
    import urllib.request

    from aios_core.deploy.deployer import deploy
    from aios_core.deploy.store import ProjectStore

    _write_min_app(tmp_path)
    (tmp_path / "project.json").write_text('{"run": ["python", "app.py"], "port": 8000}')
    store = ProjectStore(path=tmp_path / "projects.json")
    result = deploy("dep1", tmp_path, store=store)
    project = Project(slug="dep1", source_dir=tmp_path, spec=Spec(run=["python", "app.py"], port=8000))
    try:
        assert result["status"] == "running", result
        with urllib.request.urlopen(result["url"], timeout=5) as r:
            assert "deploy-supervisor-ok" in r.read().decode()
        assert store.get("dep1").status == "running"
    finally:
        Supervisor().stop(project)


def test_deploy_reports_failure_with_logs(tmp_path):
    """A crashing app yields status=error WITH the container logs, so an agent can
    read the failure and fix it — the feedback loop that makes deploy non-blind."""
    from aios_core.deploy.deployer import deploy

    (tmp_path / "app.py").write_text("print('BOOMCRASH', flush=True)\nimport sys; sys.exit(1)\n")
    (tmp_path / "project.json").write_text('{"run": ["python", "app.py"], "port": 8000}')
    result = deploy("dep2", tmp_path)
    project = Project(slug="dep2", source_dir=tmp_path, spec=Spec(run=["python", "app.py"], port=8000))
    try:
        assert result["status"] == "error"
        assert "BOOMCRASH" in result.get("logs", "")  # real crash output surfaced
    finally:
        Supervisor().stop(project)


def test_deploy_missing_manifest(tmp_path):
    """No project.json → a clear error (no container started)."""
    from aios_core.deploy.deployer import deploy

    _write_min_app(tmp_path)
    result = deploy("dep3", tmp_path)
    assert result["status"] == "error" and "project.json" in result["error"]


def test_deployed_container_is_hardened(tmp_path):
    """The deployed container really has the hardening applied (docker inspect):
    all caps dropped, no-new-privileges, and resource limits set."""
    import json as _json
    import subprocess

    from aios_core.deploy.deployer import deploy

    _write_min_app(tmp_path)
    (tmp_path / "project.json").write_text('{"run": ["python", "app.py"], "port": 8000}')
    result = deploy("hard1", tmp_path)
    project = Project(slug="hard1", source_dir=tmp_path, spec=Spec(run=["python", "app.py"], port=8000))
    try:
        assert result["status"] == "running", result
        inspect = subprocess.run(
            ["docker", "inspect", "aios-app-hard1"], capture_output=True, text=True
        )
        host = _json.loads(inspect.stdout)[0]["HostConfig"]
        assert "ALL" in (host.get("CapDrop") or [])
        assert any("no-new-privileges" in opt for opt in (host.get("SecurityOpt") or []))
        assert host.get("Memory", 0) > 0
        assert host.get("PidsLimit", 0) == 256
    finally:
        Supervisor().stop(project)


def test_deploy_app_with_dependencies(tmp_path):
    """A requirements.txt is pip-installed at container start — a real Flask app
    (non-stdlib) deploys and serves. Slower (pip install), but proves real apps work."""
    import urllib.request

    from aios_core.deploy.deployer import deploy

    (tmp_path / "app.py").write_text(
        textwrap.dedent(
            """
            from flask import Flask
            app = Flask(__name__)
            @app.route("/")
            def home():
                return "FLASK-DEP-OK"
            app.run(host="0.0.0.0", port=8000)
            """
        )
    )
    (tmp_path / "requirements.txt").write_text("flask\n")
    (tmp_path / "project.json").write_text('{"run": ["python", "app.py"], "port": 8000}')
    result = deploy("depapp", tmp_path)
    project = Project(slug="depapp", source_dir=tmp_path, spec=Spec(run=["python", "app.py"], port=8000))
    try:
        assert result["status"] == "running", result
        with urllib.request.urlopen(result["url"], timeout=5) as r:
            assert "FLASK-DEP-OK" in r.read().decode()
    finally:
        Supervisor().stop(project)
