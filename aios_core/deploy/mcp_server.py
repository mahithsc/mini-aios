"""MCP server exposing `deploy` to a Codex session (step 5b).

Codex is launched (by codex_start) with this registered as an MCP server, so
mid-session — after it has written the code + project.json — Codex can call
`deploy(slug)` and get structured feedback (url on success, or error + container
logs to fix and retry). The server runs host-side over stdio (Codex spawns it);
it deploys the project in Codex's working directory via the Supervisor.

Run standalone (how Codex launches it):
    python -m aios_core.deploy.mcp_server
"""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from .deployer import deploy as _deploy
from .store import ProjectStore

mcp = FastMCP("aios-deploy")


@mcp.tool()
def deploy(slug: str) -> dict:
    """Build and run the project in the current directory as a live service.

    Requires a project.json in the working directory:
    {"run": ["python","app.py"], "port": 8000, "image": "python:3.12-slim"}.
    Returns {status:"running", url, ...} on success, or {status:"error", error,
    logs} on failure — read `logs` to fix the app and call deploy again.
    """
    return _deploy(slug, os.getcwd(), store=ProjectStore())


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
