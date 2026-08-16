"""deploy MCP server (step 5b) — real MCP protocol round-trip.

Honest end-to-end of the MCP layer: the server is spawned over stdio exactly as
Codex launches it, we do the real MCP handshake, list tools, and call `deploy` —
which builds+runs a REAL container that we then fetch over HTTP. No mocks.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import textwrap
from pathlib import Path

import pytest

from aios_core.deploy.models import Project, Spec
from aios_core.deploy.supervisor import Supervisor, docker_available

pytestmark = pytest.mark.skipif(not docker_available(), reason="Docker not available")

REPO_ROOT = str(Path(__file__).resolve().parent.parent)


def _write_app(dir_path: Path):
    (dir_path / "app.py").write_text(
        textwrap.dedent(
            """
            from http.server import BaseHTTPRequestHandler, HTTPServer
            class H(BaseHTTPRequestHandler):
                def do_GET(self):
                    self.send_response(200); self.end_headers()
                    self.wfile.write(b"deploy-mcp-ok")
                def log_message(self, *a):
                    pass
            HTTPServer(("0.0.0.0", 8000), H).serve_forever()
            """
        )
    )
    (dir_path / "project.json").write_text(
        '{"run": ["python", "app.py"], "port": 8000}'
    )


def _extract(result) -> dict:
    sc = getattr(result, "structuredContent", None)
    if isinstance(sc, dict):
        return sc["result"] if set(sc.keys()) == {"result"} else sc
    for c in getattr(result, "content", []) or []:
        if getattr(c, "type", "") == "text":
            try:
                return json.loads(c.text)
            except json.JSONDecodeError:
                continue
    return {}


async def _deploy_via_mcp(cwd: str, slug: str):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = dict(os.environ)
    env["PYTHONPATH"] = REPO_ROOT
    env["AIOS_ENABLE_LEGACY_LOCAL_DEPLOY"] = "1"
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "aios_core.deploy.mcp_server"],
        cwd=cwd,
        env=env,
    )
    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        tools = await session.list_tools()
        result = await session.call_tool("deploy", {"slug": slug})
        return [t.name for t in tools.tools], _extract(result)


def test_deploy_via_mcp_roundtrip(tmp_path):
    _write_app(tmp_path)
    tool_names, payload = asyncio.run(_deploy_via_mcp(str(tmp_path), "mcp1"))
    project = Project(
        slug="mcp1", source_dir=tmp_path, spec=Spec(run=["python", "app.py"], port=8000)
    )
    try:
        assert "deploy" in tool_names  # Codex would see this tool
        assert "deploy_database" in tool_names
        assert "deploy_server" in tool_names
        assert "deploy_frontend" in tool_names
        assert "get_deployment_status" in tool_names
        assert "get_deployment_events" in tool_names
        assert "get_app_info" in tool_names
        assert "cancel_cloud_deployment" in tool_names
        assert "resume_cloud_deployment" in tool_names
        assert "rollback_cloud_deployment" in tool_names
        assert "delete_cloud_app" in tool_names
        assert "list_database_tables" in tool_names
        assert "inspect_database_table" in tool_names
        assert "query_database_table" in tool_names
        assert "list_database_migrations" in tool_names
        assert payload.get("status") == "running", payload
        import urllib.request

        with urllib.request.urlopen(payload["url"], timeout=5) as r:
            assert "deploy-mcp-ok" in r.read().decode()  # really deployed via MCP
    finally:
        Supervisor().stop(project)
