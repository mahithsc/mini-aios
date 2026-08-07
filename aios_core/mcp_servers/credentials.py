from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from ..integrations.google import GoogleOAuthService


class AccessTokenProvider(Protocol):
    """Boundary between an MCP server and a provider credential owner."""

    async def access_token(
        self,
        *,
        required_scopes: Iterable[str],
        rejected_token: str | None = None,
    ) -> str:
        """Return a token, refreshing if rejected_token is still current."""


class MiniAIOSGoogleTokenProvider:
    """Issue Google access tokens through mini-AIOS's local OAuth service."""

    def __init__(self, service: GoogleOAuthService | None = None) -> None:
        self._service = service or GoogleOAuthService()

    async def access_token(
        self,
        *,
        required_scopes: Iterable[str],
        rejected_token: str | None = None,
    ) -> str:
        return await self._service.valid_access_token(
            required_scopes=required_scopes,
            rejected_token=rejected_token,
        )
