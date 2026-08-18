"""Opt-in full loop: real Pi authors an app and deploys it through aios-cloud."""

from __future__ import annotations

import json
import os
import shutil
import time
import urllib.request

import pytest

from aios_core.deploy.cloud_client import CloudDeployClient

_APP_ID = os.getenv("PI_DEPLOY_E2E_APP_ID", "")
_MARKER = "HELLO-FROM-PI-CLOUD-E2E"

pytestmark = pytest.mark.skipif(
    not (
        shutil.which("pi")
        and os.getenv("PI_DEPLOY_E2E")
        and _APP_ID
        and os.getenv("AIOS_CLOUD_DEVICE_TOKEN")
    ),
    reason=(
        "set PI_DEPLOY_E2E=1, PI_DEPLOY_E2E_APP_ID, and "
        "AIOS_CLOUD_DEVICE_TOKEN (uses Pi auth, network, model spend, and cloud resources)"
    ),
)

_TASK = f"""
Build and deploy a static frontend in the current directory for app ID {_APP_ID!r}.
Create frontend/index.html containing exactly the visible marker {_MARKER!r} and a
version-1 aios.deploy.yaml that declares only the frontend component with source
`frontend`. Test the files locally, call the trusted `deploy` tool, and then call
`deployment_status` until the returned pipeline is terminal. Do not use project.json,
Docker, a provider CLI, or any direct provider API. Finish only when the cloud reports
success; otherwise return the exact deployment error.
""".strip()


def test_pi_builds_and_deploys_through_cloud(tmp_path, monkeypatch) -> None:
    from aios_core.tools.pi import pi
    from aios_core.tools.pi_job import close_all_pi_jobs

    (tmp_path / ".aios-app.json").write_text(
        json.dumps({"version": 1, "app_id": _APP_ID, "name": "Pi cloud e2e"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("AIOS_PI_ALLOWED_ROOTS", str(tmp_path))
    started = pi(action="start", task=_TASK, path=str(tmp_path))
    assert "job_id" in started, started
    job_id = started["job_id"]

    cursor, final = 0, None
    deadline = time.time() + 600
    try:
        while time.time() < deadline:
            response = pi(action="poll", job_id=job_id, cursor=cursor, wait=15)
            cursor = response["cursor"]
            if response["status"] not in {"starting", "running", "stopping"}:
                final = response
                break

        assert final is not None and final["status"] == "done", final
        info = CloudDeployClient().get_app_info(_APP_ID)
        frontend = info.get("components", {}).get("frontend", {})
        url = frontend.get("url")
        assert isinstance(url, str) and url.startswith("https://"), info
        with urllib.request.urlopen(url, timeout=20) as response:
            body = response.read().decode()
        assert _MARKER in body
    finally:
        close_all_pi_jobs()
