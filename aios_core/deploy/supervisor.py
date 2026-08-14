"""Supervisor: run a Project as a real container and observe it.

Step 1 of the deploy build (see docs/deploy-supervisor-PLAN.md): spec -> `docker
run` a container, publish a loopback port, health-check, logs, stop/remove.

This first cut mounts the source read-only and runs the spec's command in a base
image (fast to iterate). The apps-infra container hardening (content-addressed
snapshot, dropped caps, no host mounts, resource limits) is grafted in at the end
of step 1 — this module is the seam where that goes.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import time
import urllib.request

from .models import Project

CONTAINER_PREFIX = "aios-app-"


class SupervisorError(Exception):
    pass


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(
            ["docker", "info"], capture_output=True, text=True, timeout=15
        ).returncode == 0
    except Exception:
        return False


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _docker(*args: str, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


class Supervisor:
    def container_name(self, slug: str) -> str:
        return f"{CONTAINER_PREFIX}{slug}"

    def is_running(self, project: Project) -> bool:
        name = self.container_name(project.slug)
        res = _docker("ps", "-q", "-f", f"name=^{name}$")
        return bool(res.stdout.strip())

    def start(self, project: Project) -> dict:
        """Start (or restart) the project's container; return a runtime handle."""
        name = self.container_name(project.slug)
        self.stop(project)  # idempotent: clear any stale container with this name
        host_port = _free_port()
        env_args: list[str] = []
        for key, value in project.spec.env.items():
            env_args += ["-e", f"{key}={value}"]
        args = [
            "run", "-d", "--name", name,
            "-p", f"127.0.0.1:{host_port}:{project.spec.port}",
            "-v", f"{project.source_dir}:/app:ro",
            "-w", "/app",
            *env_args,
            project.spec.image,
            *project.spec.run,
        ]
        res = _docker(*args)
        if res.returncode != 0:
            raise SupervisorError(f"docker run failed: {res.stderr.strip() or res.stdout.strip()}")
        return {
            "slug": project.slug,
            "container_id": res.stdout.strip(),
            "host_port": host_port,
            "url": f"http://127.0.0.1:{host_port}",
            "status": "running",
        }

    def health(self, url: str, path: str = "/", attempts: int = 40, delay: float = 0.3) -> tuple[bool, str]:
        """Poll the app's URL until it answers (or attempts run out)."""
        target = url.rstrip("/") + path
        for _ in range(attempts):
            try:
                with urllib.request.urlopen(target, timeout=2) as resp:
                    return True, resp.read().decode(errors="replace")
            except Exception:
                time.sleep(delay)
        return False, ""

    def logs(self, project: Project, tail: int = 200) -> str:
        res = _docker("logs", "--tail", str(tail), self.container_name(project.slug))
        return (res.stdout + res.stderr).strip()

    def stop(self, project: Project) -> None:
        _docker("rm", "-f", self.container_name(project.slug))
