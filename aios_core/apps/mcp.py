from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping

from agno.tools.mcp import MCPTools
from mcp import StdioServerParameters

from .coordinator import AppCoordinator
from .models import AppRecord, McpServerSpec
from .runtime import AppRuntime


def _tool_prefix(app: AppRecord, server: McpServerSpec) -> str:
    value = f"{app.slug}_{server.id}".replace("-", "_")
    normalized = re.sub(r"[^A-Za-z0-9_]", "_", value)
    identity = f"{app.id}\0{server.id}".encode()
    suffix = hashlib.sha256(identity).hexdigest()[:8]
    return f"{normalized}_{suffix}"


class AppMCPTools(MCPTools):
    """Agno toolkit whose stdio server is a constrained Docker process."""

    def __init__(
        self,
        *,
        app: AppRecord,
        server: McpServerSpec,
        runtime: AppRuntime,
        verified_snapshot: object,
    ) -> None:
        raw = runtime.mcp_server_parameters(
            app,
            server,
            network_approved=app.network_approved,
            verified_snapshot=verified_snapshot,
        )
        if not isinstance(raw, Mapping):
            raise TypeError("App runtime returned invalid MCP server parameters")
        command = raw.get("command")
        args = raw.get("args", [])
        env = raw.get("env")
        cwd = raw.get("cwd")
        if not isinstance(command, str) or not command:
            raise ValueError("App runtime MCP command is missing")
        if not isinstance(args, list) or not all(
            isinstance(argument, str) for argument in args
        ):
            raise ValueError("App runtime MCP args must be a string array")
        if env is not None and not isinstance(env, dict):
            raise ValueError("App runtime MCP env must be an object")
        if cwd is not None and not isinstance(cwd, str):
            raise ValueError("App runtime MCP cwd must be a string")
        self.app_id = app.id
        self.app_slug = app.slug
        self.server_id = server.id
        super().__init__(
            transport="stdio",
            server_params=StdioServerParameters(
                command=command,
                args=args,
                env=env,
                cwd=cwd,
            ),
            timeout_seconds=30,
            tool_name_prefix=_tool_prefix(app, server),
        )


def get_enabled_app_mcp_toolkits() -> list[MCPTools]:
    """Build each enabled App MCP independently so one failure stays isolated."""

    try:
        coordinator = AppCoordinator()
        apps = coordinator.registry.list(enabled=True)
    except Exception as exc:  # noqa: BLE001 - Apps are optional at agent startup
        print(f"[apps] registry could not be loaded: {exc}")
        return []

    toolkits: list[MCPTools] = []
    for app in apps:
        try:
            if not app.active_hash:
                continue
            verified_snapshot = coordinator.service.verify_snapshot(
                app,
                app.active_hash,
            )
            manifest = coordinator.active_manifest(app)
        except Exception as exc:  # noqa: BLE001 - isolate a broken App
            print(f"[apps] active snapshot for {app.slug} could not be loaded: {exc}")
            continue
        if manifest is None:
            continue
        for server in manifest.mcp_servers:
            try:
                toolkits.append(
                    AppMCPTools(
                        app=app,
                        server=server,
                        runtime=coordinator.runtime,
                        verified_snapshot=verified_snapshot,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - isolate one MCP server
                print(
                    f"[apps] MCP server {app.slug}/{server.id} could not be loaded: {exc}"
                )
    return toolkits
