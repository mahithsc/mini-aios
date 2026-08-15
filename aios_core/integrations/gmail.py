"""Backward-compatible Gmail names for the shared Google connection."""

from .google import (
    DEFAULT_GMAIL_TOOLS,
    GMAIL_READ_SCOPES,
    GMAIL_SEND_SCOPES,
    GMAIL_SCOPES,
    CredentialCipher,
    GoogleConfig,
    GoogleCredentialStore,
    GoogleIntegrationError,
    GoogleOAuthService,
    GoogleTokens,
)

GMAIL_PROVIDER = "google"
DEFAULT_GMAIL_SCOPES = GMAIL_SCOPES
GmailConfig = GoogleConfig
GmailCredentialStore = GoogleCredentialStore
GmailIntegrationError = GoogleIntegrationError
GmailOAuthService = GoogleOAuthService
GmailTokens = GoogleTokens

__all__ = [
    "CredentialCipher",
    "DEFAULT_GMAIL_SCOPES",
    "DEFAULT_GMAIL_TOOLS",
    "GMAIL_PROVIDER",
    "GMAIL_READ_SCOPES",
    "GMAIL_SEND_SCOPES",
    "GmailConfig",
    "GmailCredentialStore",
    "GmailIntegrationError",
    "GmailOAuthService",
    "GmailTokens",
]
