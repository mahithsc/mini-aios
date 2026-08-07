"""Read-only Gmail MCP server owned and run by mini-AIOS."""

from .client import GmailAPIClient, GmailAPIError

__all__ = ["GmailAPIClient", "GmailAPIError"]
