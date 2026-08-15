"""Backward-compatible imports for the shared Google MCP toolkits."""

from .google_mcp import (
    GoogleMCPTools,
    LocalGmailMCPTools,
    get_calendar_mcp_toolkit,
    get_gmail_mcp_toolkit,
    get_google_mcp_toolkits,
)

GmailMCPTools = LocalGmailMCPTools

__all__ = [
    "GmailMCPTools",
    "GoogleMCPTools",
    "LocalGmailMCPTools",
    "get_calendar_mcp_toolkit",
    "get_gmail_mcp_toolkit",
    "get_google_mcp_toolkits",
]
