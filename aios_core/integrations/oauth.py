from __future__ import annotations

import base64
import hashlib
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx

HttpClientFactory = Callable[[], httpx.AsyncClient]


class OAuthProtocolError(RuntimeError):
    """Provider-neutral OAuth failure with a stable error code."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PublicOAuthProvider:
    """Public OAuth metadata that is safe to ship with an installed app."""

    id: str
    client_profile: str
    client_id: str
    authorization_endpoint: str
    token_endpoint: str
    redirect_uri: str
    revocation_endpoint: str | None = None
    authorization_parameters: Mapping[str, str] = field(default_factory=dict)

    @property
    def configured(self) -> bool:
        return bool(
            self.id
            and self.client_profile
            and self.client_id
            and self.authorization_endpoint
            and self.token_endpoint
            and self.redirect_uri
        )


@dataclass(frozen=True)
class PkceMaterial:
    verifier: str
    challenge: str


def create_pkce_material() -> PkceMaterial:
    """Create RFC 7636 S256 material for one authorization attempt."""

    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return PkceMaterial(verifier=verifier, challenge=challenge)


def create_oauth_state() -> str:
    return secrets.token_urlsafe(32)


def hash_oauth_state(state: str) -> str:
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


def build_authorization_url(
    provider: PublicOAuthProvider,
    *,
    scopes: tuple[str, ...],
    state: str,
    code_challenge: str,
) -> str:
    _require_configured(provider)
    parameters = {
        "client_id": provider.client_id,
        "redirect_uri": provider.redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        **provider.authorization_parameters,
    }
    return f"{provider.authorization_endpoint}?{urlencode(parameters)}"


class PublicOAuthTokenClient:
    """Authorization-code + PKCE client that never uses a client secret."""

    def __init__(
        self,
        provider: PublicOAuthProvider,
        *,
        http_client_factory: HttpClientFactory | None = None,
    ) -> None:
        self.provider = provider
        self._http_client_factory = http_client_factory or (
            lambda: httpx.AsyncClient(timeout=15.0)
        )

    async def exchange_code(
        self,
        *,
        code: str,
        code_verifier: str,
        redirect_uri: str,
    ) -> dict[str, Any]:
        if redirect_uri != self.provider.redirect_uri:
            raise OAuthProtocolError(
                "OAuth redirect URI does not match the authorization session",
                code="redirect_uri_mismatch",
            )
        return await self._post_form(
            self.provider.token_endpoint,
            {
                "client_id": self.provider.client_id,
                "code": code,
                "code_verifier": code_verifier,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
        )

    async def refresh(self, refresh_token: str) -> dict[str, Any]:
        return await self._post_form(
            self.provider.token_endpoint,
            {
                "client_id": self.provider.client_id,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
        )

    async def revoke(self, token: str) -> None:
        if self.provider.revocation_endpoint is None:
            return
        await self._post_form(
            self.provider.revocation_endpoint,
            {"token": token},
            expect_json=False,
        )

    async def _post_form(
        self,
        url: str,
        data: Mapping[str, str],
        *,
        expect_json: bool = True,
    ) -> dict[str, Any]:
        _require_configured(self.provider)
        try:
            async with self._http_client_factory() as client:
                response = await client.post(
                    url,
                    data=data,
                    headers={
                        "Accept": "application/json",
                        "Cache-Control": "no-store",
                    },
                )
        except httpx.HTTPError as exc:
            raise OAuthProtocolError(
                f"Could not reach {self.provider.id} OAuth",
                code="oauth_unavailable",
            ) from exc
        if response.is_error:
            code, message = oauth_response_error(response)
            raise OAuthProtocolError(
                f"{self.provider.id} OAuth request failed: {message}",
                code=code,
            )
        if not expect_json:
            return {}
        try:
            payload = response.json()
        except ValueError as exc:
            raise OAuthProtocolError(
                f"{self.provider.id} OAuth returned an invalid response",
                code="invalid_oauth_response",
            ) from exc
        if not isinstance(payload, dict):
            raise OAuthProtocolError(
                f"{self.provider.id} OAuth returned an invalid response",
                code="invalid_oauth_response",
            )
        return payload


def oauth_response_error(response: httpx.Response) -> tuple[str | None, str]:
    try:
        payload = response.json()
    except ValueError:
        return None, str(response.status_code)
    if not isinstance(payload, dict):
        return None, str(response.status_code)
    code = payload.get("error")
    raw_message = payload.get("error_description") or code
    return (
        code if isinstance(code, str) else None,
        raw_message if isinstance(raw_message, str) else str(response.status_code),
    )


def _require_configured(provider: PublicOAuthProvider) -> None:
    if not provider.configured:
        raise OAuthProtocolError(
            f"{provider.id} OAuth public client is not configured",
            code="not_configured",
        )
