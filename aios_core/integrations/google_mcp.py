from __future__ import annotations

import os
import sys
from datetime import timedelta

from agno.tools.mcp import MCPTools, StreamableHTTPClientParams
from mcp import StdioServerParameters

from .google import (
    CALENDAR_SCOPES,
    DEFAULT_GMAIL_TOOLS,
    GoogleConfig,
    GoogleOAuthService,
)
from ..workspace import get_project_root

_GMAIL_SERVER_ENV_KEYS = (
    "AIOS_ENV",
    "APP_ENV",
    "ENV",
    "AIOS_HOME",
    "AIOS_STATE_DIR",
    "AIOS_CREDENTIAL_ENCRYPTION_KEY",
    "AIOS_CREDENTIAL_ENCRYPTION_KEY_FILE",
    "AIOS_GOOGLE_OAUTH_CLIENT_ID",
    "AIOS_GOOGLE_OAUTH_CLIENT_PROFILE",
    "AIOS_GOOGLE_OAUTH_REDIRECT_URI",
)


class GoogleMCPTools(MCPTools):
    """Remote Google Workspace MCP toolkit backed by a local credential."""

    def __init__(
        self,
        *,
        oauth_service: GoogleOAuthService,
        service_name: str,
        url: str,
        tools: tuple[str, ...],
        required_scopes: tuple[str, ...],
        requires_confirmation_tools: tuple[str, ...] = (),
    ) -> None:
        self.oauth_service = oauth_service
        self.service_name = service_name
        self.required_scopes = required_scopes
        self.google_mcp_url = url
        super().__init__(
            url=url,
            transport="streamable-http",
            timeout_seconds=30,
            include_tools=list(tools),
            tool_name_prefix=service_name,
            requires_confirmation_tools=list(requires_confirmation_tools),
        )

    async def connect(self, force: bool = False):
        access_token = await self.oauth_service.valid_access_token(
            required_scopes=self.required_scopes,
        )
        self.server_params = StreamableHTTPClientParams(
            url=self.google_mcp_url,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Cache-Control": "no-store",
            },
            timeout=timedelta(seconds=30),
            sse_read_timeout=timedelta(minutes=5),
        )
        return await super().connect(force=force)


def gmail_server_parameters() -> StdioServerParameters:
    """Build the local Gmail process without forwarding unrelated app secrets."""

    environment = {
        key: os.environ[key]
        for key in _GMAIL_SERVER_ENV_KEYS
        if os.getenv(key) is not None
    }
    environment["PYTHONUNBUFFERED"] = "1"
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "aios_core.mcp_servers.gmail.server"],
        env=environment,
        cwd=get_project_root(),
    )


class LocalGmailMCPTools(MCPTools):
    """Agno client for mini-AIOS's first-party local Gmail MCP process."""

    service_name = "gmail"

    def __init__(self, *, tools: tuple[str, ...] = DEFAULT_GMAIL_TOOLS) -> None:
        super().__init__(
            transport="stdio",
            server_params=gmail_server_parameters(),
            timeout_seconds=30,
            include_tools=list(tools),
            tool_name_prefix=self.service_name,
        )


def get_google_mcp_toolkits() -> list[MCPTools]:
    config = GoogleConfig.from_env()
    if not config.enabled or not config.configured:
        return []

    oauth_service = GoogleOAuthService(config=config)
    status = oauth_service.connection_status()
    if not status["connected"]:
        return []

    services = status["services"]
    toolkits: list[MCPTools] = []
    if services["gmail"]:
        toolkits.append(LocalGmailMCPTools(tools=config.gmail_tools))
    if services["calendar"]:
        toolkits.append(
            GoogleMCPTools(
                oauth_service=oauth_service,
                service_name="calendar",
                url=config.calendar_mcp_url,
                tools=config.calendar_tools,
                required_scopes=CALENDAR_SCOPES,
                requires_confirmation_tools=(
                    "create_event",
                    "update_event",
                    "respond_to_event",
                ),
            )
        )
    return toolkits


def get_gmail_mcp_toolkit() -> LocalGmailMCPTools | None:
    return next(
        (
            toolkit
            for toolkit in get_google_mcp_toolkits()
            if isinstance(toolkit, LocalGmailMCPTools)
        ),
        None,
    )


def get_calendar_mcp_toolkit() -> GoogleMCPTools | None:
    return next(
        (
            toolkit
            for toolkit in get_google_mcp_toolkits()
            if toolkit.service_name == "calendar"
        ),
        None,
    )
