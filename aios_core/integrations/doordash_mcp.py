from __future__ import annotations

import os
import sys

from agno.tools.mcp import MCPTools
from mcp import StdioServerParameters

from ..workspace import get_project_root
from .doordash import (
    DEFAULT_DOORDASH_TOOLS,
    DoorDashConfig,
)

_DOORDASH_SERVER_ENV_KEYS = (
    "AIOS_DOORDASH_CLI_PATH",
)


def doordash_server_parameters(
    config: DoorDashConfig | None = None,
) -> StdioServerParameters:
    """Build the local DoorDash process without forwarding unrelated secrets."""

    resolved_config = config or DoorDashConfig.from_env()
    environment = {
        key: os.environ[key]
        for key in _DOORDASH_SERVER_ENV_KEYS
        if os.getenv(key) is not None
    }
    if resolved_config.executable:
        environment["AIOS_DOORDASH_CLI_PATH"] = resolved_config.executable
    environment["PYTHONUNBUFFERED"] = "1"
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "aios_core.mcp_servers.doordash.server"],
        env=environment,
        cwd=get_project_root(),
    )


class LocalDoorDashMCPTools(MCPTools):
    """Agno client for mini-AIOS's first-party local DoorDash MCP process."""

    service_name = "doordash"

    def __init__(
        self,
        *,
        config: DoorDashConfig | None = None,
        tools: tuple[str, ...] = DEFAULT_DOORDASH_TOOLS,
    ) -> None:
        resolved_config = config or DoorDashConfig.from_env()
        super().__init__(
            transport="stdio",
            server_params=doordash_server_parameters(resolved_config),
            timeout_seconds=90,
            include_tools=list(tools),
            tool_name_prefix=self.service_name,
        )


def get_doordash_mcp_toolkit() -> LocalDoorDashMCPTools | None:
    """Expose dd-cli whenever installed; the CLI remains the auth authority."""

    config = DoorDashConfig.from_env()
    if not config.enabled or not config.configured:
        return None
    return LocalDoorDashMCPTools(config=config, tools=config.tools)
