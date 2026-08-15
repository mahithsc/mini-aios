from __future__ import annotations

import asyncio
import hmac
import json
import os
import secrets
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from cryptography.fernet import Fernet, InvalidToken

from .. import db
from ..workspace import get_runtime_paths
from .oauth import (
    OAuthProtocolError,
    PublicOAuthProvider,
    PublicOAuthTokenClient,
    build_authorization_url,
    create_oauth_state,
    create_pkce_material,
    hash_oauth_state,
)

GOOGLE_PROVIDER = "google"
GOOGLE_CLIENT_PROFILE = "google-ios-dev"
DEFAULT_GOOGLE_CLIENT_ID = (
    "1081270832974-cmsitu4392uhcvmg8r95jrv8to7aq635.apps.googleusercontent.com"
)
GOOGLE_AUTHORIZATION_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_REVOCATION_URL = "https://oauth2.googleapis.com/revoke"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

CALENDAR_MCP_URL = "https://calendarmcp.googleapis.com/mcp/v1"

IDENTITY_SCOPES = ("openid", "email")
GMAIL_READ_SCOPES = ("https://www.googleapis.com/auth/gmail.readonly",)
GMAIL_SEND_SCOPES = ("https://www.googleapis.com/auth/gmail.send",)
GMAIL_SCOPES = (*GMAIL_READ_SCOPES, *GMAIL_SEND_SCOPES)
CALENDAR_SCOPES = (
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
)
DEFAULT_GOOGLE_SCOPES = (*IDENTITY_SCOPES, *GMAIL_SCOPES, *CALENDAR_SCOPES)
SERVICE_APP_IDS = {
    "gmail": "gmail",
    "calendar": "google-calendar",
}

DEFAULT_GMAIL_TOOLS = (
    "search_messages",
    "get_message",
    "get_thread",
    "list_labels",
    "send_email",
)
DEFAULT_CALENDAR_TOOLS = (
    "list_events",
    "get_event",
    "list_calendars",
    "suggest_time",
    "search_events",
    "create_event",
    "update_event",
    "respond_to_event",
)

_TOKEN_REFRESH_SKEW_SECONDS = 5 * 60
_AUTHORIZATION_TTL_SECONDS = 10 * 60
_access_cache: dict[tuple[str, str], "AccessToken"] = {}
_refresh_locks: dict[tuple[str, str], asyncio.Lock] = {}


class GoogleIntegrationError(RuntimeError):
    """Raised when Google authorization or credential handling fails."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class GoogleConfig:
    client_id: str
    redirect_uri: str
    client_profile: str = GOOGLE_CLIENT_PROFILE
    authorization_endpoint: str = GOOGLE_AUTHORIZATION_URL
    token_endpoint: str = GOOGLE_TOKEN_URL
    revocation_endpoint: str = GOOGLE_REVOCATION_URL
    scopes: tuple[str, ...] = DEFAULT_GOOGLE_SCOPES
    calendar_mcp_url: str = CALENDAR_MCP_URL
    gmail_tools: tuple[str, ...] = DEFAULT_GMAIL_TOOLS
    calendar_tools: tuple[str, ...] = DEFAULT_CALENDAR_TOOLS
    enabled: bool = True

    @property
    def configured(self) -> bool:
        return self.public_provider.configured

    @property
    def public_provider(self) -> PublicOAuthProvider:
        return PublicOAuthProvider(
            id=GOOGLE_PROVIDER,
            client_profile=self.client_profile,
            client_id=self.client_id,
            authorization_endpoint=self.authorization_endpoint,
            token_endpoint=self.token_endpoint,
            redirect_uri=self.redirect_uri,
            revocation_endpoint=self.revocation_endpoint,
            authorization_parameters={
                "access_type": "offline",
                "prompt": "consent",
                "include_granted_scopes": "true",
            },
        )

    @classmethod
    def from_env(cls) -> "GoogleConfig":
        enabled_name = (
            "AIOS_GOOGLE_MCP_ENABLED"
            if os.getenv("AIOS_GOOGLE_MCP_ENABLED") is not None
            else "AIOS_GMAIL_MCP_ENABLED"
        )
        client_id = os.getenv(
            "AIOS_GOOGLE_OAUTH_CLIENT_ID",
            DEFAULT_GOOGLE_CLIENT_ID,
        ).strip()
        redirect_uri = os.getenv("AIOS_GOOGLE_OAUTH_REDIRECT_URI", "").strip()
        if not redirect_uri and client_id.endswith(".apps.googleusercontent.com"):
            redirect_uri = (
                "com.googleusercontent.apps."
                f"{client_id.removesuffix('.apps.googleusercontent.com')}"
                ":/oauthredirect"
            )
        return cls(
            client_id=client_id,
            redirect_uri=redirect_uri,
            client_profile=os.getenv(
                "AIOS_GOOGLE_OAUTH_CLIENT_PROFILE",
                GOOGLE_CLIENT_PROFILE,
            ).strip(),
            scopes=DEFAULT_GOOGLE_SCOPES,
            calendar_mcp_url=os.getenv(
                "AIOS_CALENDAR_MCP_URL",
                CALENDAR_MCP_URL,
            ).strip(),
            gmail_tools=_csv_env(
                "AIOS_GMAIL_MCP_TOOLS",
                DEFAULT_GMAIL_TOOLS,
            ),
            calendar_tools=_csv_env(
                "AIOS_CALENDAR_MCP_TOOLS",
                DEFAULT_CALENDAR_TOOLS,
            ),
            enabled=_env_bool(enabled_name, default=True),
        )


@dataclass(frozen=True)
class GoogleTokens:
    access_token: str
    refresh_token: str | None
    token_type: str
    scopes: tuple[str, ...]
    expires_at: int
    provider_account_id: str | None = None
    account_email: str | None = None


@dataclass(frozen=True)
class AccessToken:
    value: str
    expires_at: int

    @property
    def usable(self) -> bool:
        return self.expires_at > int(time.time()) + _TOKEN_REFRESH_SKEW_SECONDS


@dataclass(frozen=True)
class StoredGoogleConnection:
    connection_id: str
    owner_id: str
    refresh_token: str
    token_type: str
    scopes: tuple[str, ...]
    status: str
    provider_account_id: str | None
    account_email: str | None
    created_at: int
    updated_at: int
    last_refreshed_at: int | None
    last_error: str | None
    oauth_client_profile: str | None
    oauth_client_id: str | None
    authorized_at: int | None


@dataclass(frozen=True)
class PendingOAuthSession:
    session_id: str
    apps: tuple[str, ...]
    scopes: tuple[str, ...]
    client_profile: str
    client_id: str
    state_hash: str
    code_verifier: str
    redirect_uri: str
    status: str
    expires_at: int
    last_error: str | None


def _csv_env(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    value = os.getenv(name)
    if value is None:
        return default
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _env_bool(name: str, *, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _apps_from_rows(rows: Iterable[Any]) -> set[str]:
    apps: set[str] = set()
    for row in rows:
        try:
            values = json.loads(row[0])
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(values, list):
            apps.update(value for value in values if isinstance(value, str))
    return apps


def scopes_for_services(
    services: Iterable[str],
    *,
    allowed_scopes: tuple[str, ...] = DEFAULT_GOOGLE_SCOPES,
) -> tuple[str, ...]:
    requested = {service.strip().lower() for service in services}
    if not requested or not requested.issubset({"gmail", "calendar"}):
        raise GoogleIntegrationError(
            "Google services must contain gmail and/or calendar",
            code="invalid_services",
        )
    candidates: list[str] = list(IDENTITY_SCOPES)
    if "gmail" in requested:
        candidates.extend(GMAIL_SCOPES)
    if "calendar" in requested:
        candidates.extend(CALENDAR_SCOPES)
    allowed = set(allowed_scopes)
    return tuple(scope for scope in candidates if scope in allowed)


class CredentialCipher:
    """Encrypt credentials with a key that is persisted outside SQLite."""

    def __init__(self, key: str | bytes | None = None) -> None:
        if key is None:
            configured_key = os.getenv("AIOS_CREDENTIAL_ENCRYPTION_KEY", "").strip()
            configured_key_file = os.getenv(
                "AIOS_CREDENTIAL_ENCRYPTION_KEY_FILE",
                "",
            ).strip()
            if configured_key:
                key = configured_key.encode("ascii")
            elif configured_key_file:
                try:
                    key = Path(configured_key_file).read_bytes().strip()
                except OSError as exc:
                    raise GoogleIntegrationError(
                        "AIOS credential encryption key file could not be read",
                        code="invalid_encryption_key",
                    ) from exc
            else:
                key = self._load_key()
        elif isinstance(key, str):
            key = key.encode("ascii")

        try:
            self._fernet = Fernet(key)
        except (TypeError, ValueError) as exc:
            raise GoogleIntegrationError(
                "AIOS_CREDENTIAL_ENCRYPTION_KEY must be a valid Fernet key",
                code="invalid_encryption_key",
            ) from exc

    @staticmethod
    def _load_key() -> bytes:
        key_path = get_runtime_paths().state / "credentials.key"
        key_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            key = key_path.read_bytes().strip()
        except FileNotFoundError:
            key = Fernet.generate_key()
            if not CredentialCipher._write_private_file(key_path, key + b"\n"):
                key = key_path.read_bytes().strip()
        return key

    @staticmethod
    def _write_private_file(path: Path, content: bytes) -> bool:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError:
            return False
        with os.fdopen(descriptor, "wb") as file:
            file.write(content)
        path.chmod(0o600)
        return True

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError) as exc:
            raise GoogleIntegrationError(
                "Stored Google credentials could not be decrypted",
                code="credentials_unavailable",
            ) from exc


class GoogleCredentialStore:
    """Google credential storage in the shared mini-aios SQLite database."""

    def __init__(
        self,
        *,
        owner_id: str = "local",
        db_path: str | None = None,
        cipher: CredentialCipher | None = None,
    ) -> None:
        self.owner_id = owner_id
        self.db_path = db_path or db.DB_PATH
        self.cipher = cipher or CredentialCipher()
        self._initialize()

    def _initialize(self) -> None:
        db.initialize_app_db(self.db_path)
        with db.get_db_connection(self.db_path) as connection:
            existing_session_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(integration_auth_sessions)"
                )
            }
            required_session_columns = {
                "session_id",
                "owner_id",
                "provider",
                "apps_json",
                "scopes_json",
                "client_profile",
                "client_id",
                "state_hash",
                "encrypted_code_verifier",
                "redirect_uri",
                "status",
                "expires_at",
                "created_at",
                "updated_at",
                "last_error",
            }
            if (
                existing_session_columns
                and existing_session_columns != required_session_columns
            ):
                # OAuth attempts are short-lived and contain no durable grant.
                # Recreate incompatible pre-release schemas instead of trying
                # to retain stale PKCE material.
                connection.execute("DROP TABLE integration_auth_sessions")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS integration_connections (
                    id                      TEXT PRIMARY KEY,
                    owner_id                TEXT NOT NULL,
                    provider                TEXT NOT NULL,
                    provider_account_id     TEXT,
                    account_email           TEXT,
                    encrypted_refresh_token TEXT NOT NULL,
                    token_type              TEXT NOT NULL,
                    scopes_json             TEXT NOT NULL,
                    status                  TEXT NOT NULL,
                    key_version             INTEGER NOT NULL DEFAULT 1,
                    created_at              INTEGER NOT NULL,
                    updated_at              INTEGER NOT NULL,
                    last_refreshed_at        INTEGER,
                    last_error              TEXT,
                    oauth_client_profile    TEXT,
                    oauth_client_id         TEXT,
                    authorized_at           INTEGER,
                    UNIQUE(owner_id, provider)
                );

                CREATE INDEX IF NOT EXISTS idx_integration_connections_owner
                    ON integration_connections(owner_id, provider);

                CREATE TABLE IF NOT EXISTS integration_apps (
                    owner_id       TEXT NOT NULL,
                    app_id         TEXT NOT NULL,
                    provider       TEXT NOT NULL,
                    connection_id  TEXT,
                    status         TEXT NOT NULL,
                    created_at     INTEGER NOT NULL,
                    updated_at     INTEGER NOT NULL,
                    last_error     TEXT,
                    PRIMARY KEY(owner_id, app_id)
                );

                CREATE INDEX IF NOT EXISTS idx_integration_apps_provider
                    ON integration_apps(owner_id, provider);

                CREATE TABLE IF NOT EXISTS integration_auth_sessions (
                    session_id              TEXT PRIMARY KEY,
                    owner_id                TEXT NOT NULL,
                    provider                TEXT NOT NULL,
                    apps_json               TEXT NOT NULL,
                    scopes_json             TEXT NOT NULL,
                    client_profile          TEXT NOT NULL,
                    client_id               TEXT NOT NULL,
                    state_hash              TEXT NOT NULL,
                    encrypted_code_verifier TEXT NOT NULL,
                    redirect_uri            TEXT NOT NULL,
                    status                  TEXT NOT NULL,
                    expires_at              INTEGER NOT NULL,
                    created_at              INTEGER NOT NULL,
                    updated_at              INTEGER NOT NULL,
                    last_error              TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_integration_auth_sessions_owner
                    ON integration_auth_sessions(
                        owner_id, provider, created_at DESC
                    );
                """
            )
            self._ensure_connection_columns(connection)
            self._migrate_legacy_gmail_credential(connection)

    @staticmethod
    def _ensure_connection_columns(connection) -> None:
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(integration_connections)")
        }
        additions = {
            "oauth_client_profile": "TEXT",
            "oauth_client_id": "TEXT",
            "authorized_at": "INTEGER",
        }
        for name, column_type in additions.items():
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE integration_connections "
                    f"ADD COLUMN {name} {column_type}"
                )

    def _migrate_legacy_gmail_credential(
        self,
        connection,
    ) -> None:
        """Move the earlier Gmail-only row without ever copying its access token."""
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "integration_credentials" not in tables:
            return
        already_migrated = connection.execute(
            """
            SELECT 1 FROM integration_connections
            WHERE owner_id = ? AND provider = ?
            """,
            (self.owner_id, GOOGLE_PROVIDER),
        ).fetchone()
        if already_migrated is not None:
            return
        row = connection.execute(
            """
            SELECT refresh_token, token_type, scopes_json, created_at, updated_at
            FROM integration_credentials
            WHERE provider IN ('gmail', 'google')
            ORDER BY updated_at DESC
            LIMIT 1
            """
        ).fetchone()
        if row is None or not row[0]:
            return
        try:
            refresh_token = self.cipher.decrypt(row[0])
        except GoogleIntegrationError:
            return
        now = int(time.time())
        connection.execute(
            """
            INSERT INTO integration_connections (
                id, owner_id, provider, encrypted_refresh_token, token_type,
                scopes_json, status, key_version, created_at, updated_at,
                last_refreshed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 'connected', 1, ?, ?, ?)
            """,
            (
                secrets.token_urlsafe(18),
                self.owner_id,
                GOOGLE_PROVIDER,
                self.cipher.encrypt(refresh_token),
                row[1] or "Bearer",
                row[2] or "[]",
                int(row[3] or now),
                int(row[4] or now),
                now,
            ),
        )
        connection.execute("PRAGMA secure_delete = ON")
        connection.execute(
            "DELETE FROM integration_credentials WHERE provider IN ('gmail', 'google')"
        )

    def save_tokens(
        self,
        tokens: GoogleTokens,
        *,
        oauth_client_profile: str | None = None,
        oauth_client_id: str | None = None,
        authorized_at: int | None = None,
    ) -> StoredGoogleConnection:
        now = int(time.time())
        existing = self.load_connection()
        refresh_token = tokens.refresh_token or (
            existing.refresh_token if existing is not None else None
        )
        if not refresh_token:
            raise GoogleIntegrationError(
                "Google did not return a refresh token; reconnect with consent",
                code="missing_refresh_token",
            )
        connection_id = (
            existing.connection_id
            if existing is not None
            else secrets.token_urlsafe(18)
        )
        created_at = existing.created_at if existing is not None else now
        provider_account_id = tokens.provider_account_id or (
            existing.provider_account_id if existing is not None else None
        )
        account_email = tokens.account_email or (
            existing.account_email if existing is not None else None
        )
        oauth_client_profile = oauth_client_profile or (
            existing.oauth_client_profile if existing is not None else None
        )
        oauth_client_id = oauth_client_id or (
            existing.oauth_client_id if existing is not None else None
        )
        authorized_at = authorized_at or (
            existing.authorized_at if existing is not None else now
        )
        with db.get_db_connection(self.db_path) as connection:
            connection.execute(
                """
                INSERT INTO integration_connections (
                    id, owner_id, provider, provider_account_id, account_email,
                    encrypted_refresh_token, token_type, scopes_json, status,
                    key_version, created_at, updated_at, last_refreshed_at,
                    last_error, oauth_client_profile, oauth_client_id,
                    authorized_at
                )
                VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, 'connected', 1, ?, ?, ?, NULL,
                    ?, ?, ?
                )
                ON CONFLICT(owner_id, provider) DO UPDATE SET
                    provider_account_id = excluded.provider_account_id,
                    account_email = excluded.account_email,
                    encrypted_refresh_token = excluded.encrypted_refresh_token,
                    token_type = excluded.token_type,
                    scopes_json = excluded.scopes_json,
                    status = 'connected',
                    updated_at = excluded.updated_at,
                    last_refreshed_at = excluded.last_refreshed_at,
                    last_error = NULL,
                    oauth_client_profile = excluded.oauth_client_profile,
                    oauth_client_id = excluded.oauth_client_id,
                    authorized_at = excluded.authorized_at
                """,
                (
                    connection_id,
                    self.owner_id,
                    GOOGLE_PROVIDER,
                    provider_account_id,
                    account_email,
                    self.cipher.encrypt(refresh_token),
                    tokens.token_type,
                    json.dumps(tokens.scopes),
                    created_at,
                    now,
                    now,
                    oauth_client_profile,
                    oauth_client_id,
                    authorized_at,
                ),
            )
        saved = self.load_connection()
        if saved is None:
            raise GoogleIntegrationError("Google credential could not be saved")
        if all(scope in saved.scopes for scope in GMAIL_SCOPES):
            self.set_app_status(
                "gmail",
                "connected",
                connection_id=saved.connection_id,
            )
        if any(scope in saved.scopes for scope in CALENDAR_SCOPES):
            self.set_app_status(
                "google-calendar",
                "connected",
                connection_id=saved.connection_id,
            )
        return saved

    def load_connection(self) -> StoredGoogleConnection | None:
        with db.get_db_connection(self.db_path) as connection:
            row = connection.execute(
                """
                SELECT id, owner_id, encrypted_refresh_token, token_type,
                       scopes_json, status, provider_account_id, account_email,
                       created_at, updated_at, last_refreshed_at, last_error,
                       oauth_client_profile, oauth_client_id, authorized_at
                FROM integration_connections
                WHERE owner_id = ? AND provider = ?
                """,
                (self.owner_id, GOOGLE_PROVIDER),
            ).fetchone()
        if row is None:
            return None
        try:
            scopes = tuple(json.loads(row[4]))
        except (TypeError, json.JSONDecodeError):
            scopes = ()
        return StoredGoogleConnection(
            connection_id=row[0],
            owner_id=row[1],
            refresh_token=self.cipher.decrypt(row[2]),
            token_type=row[3],
            scopes=scopes,
            status=row[5],
            provider_account_id=row[6],
            account_email=row[7],
            created_at=int(row[8]),
            updated_at=int(row[9]),
            last_refreshed_at=int(row[10]) if row[10] is not None else None,
            last_error=row[11],
            oauth_client_profile=row[12],
            oauth_client_id=row[13],
            authorized_at=int(row[14]) if row[14] is not None else None,
        )

    def set_status(self, status: str, *, error: str | None = None) -> None:
        with db.get_db_connection(self.db_path) as connection:
            connection.execute(
                """
                UPDATE integration_connections
                SET status = ?, last_error = ?, updated_at = ?
                WHERE owner_id = ? AND provider = ?
                """,
                (
                    status,
                    error,
                    int(time.time()),
                    self.owner_id,
                    GOOGLE_PROVIDER,
                ),
            )
        if status in {"reauth_required", "error"}:
            with db.get_db_connection(self.db_path) as connection:
                connection.execute(
                    """
                    UPDATE integration_apps
                    SET status = 'error', last_error = ?, updated_at = ?
                    WHERE owner_id = ? AND provider = ?
                      AND connection_id IS NOT NULL
                    """,
                    (
                        error,
                        int(time.time()),
                        self.owner_id,
                        GOOGLE_PROVIDER,
                    ),
                )

    def delete_connection(self) -> None:
        now = int(time.time())
        with db.get_db_connection(self.db_path) as connection:
            connection.execute("PRAGMA secure_delete = ON")
            connection.execute(
                """
                DELETE FROM integration_connections
                WHERE owner_id = ? AND provider = ?
                """,
                (self.owner_id, GOOGLE_PROVIDER),
            )
            connection.execute(
                """
                UPDATE integration_apps
                SET status = 'disconnected', connection_id = NULL,
                    last_error = NULL, updated_at = ?
                WHERE owner_id = ? AND provider = ?
                """,
                (now, self.owner_id, GOOGLE_PROVIDER),
            )
        _access_cache.pop((self.owner_id, GOOGLE_PROVIDER), None)

    def save_oauth_session(
        self,
        *,
        session_id: str,
        apps: tuple[str, ...],
        scopes: tuple[str, ...],
        client_profile: str,
        client_id: str,
        state_hash: str,
        code_verifier: str,
        redirect_uri: str,
        expires_at: int,
    ) -> PendingOAuthSession:
        now = int(time.time())
        with db.get_db_connection(self.db_path) as connection:
            connection.execute(
                """
                INSERT INTO integration_auth_sessions (
                    session_id, owner_id, provider, apps_json, scopes_json,
                    client_profile, client_id, state_hash,
                    encrypted_code_verifier, redirect_uri, status, expires_at,
                    created_at, updated_at, last_error
                )
                VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, NULL
                )
                """,
                (
                    session_id,
                    self.owner_id,
                    GOOGLE_PROVIDER,
                    json.dumps(apps),
                    json.dumps(scopes),
                    client_profile,
                    client_id,
                    state_hash,
                    self.cipher.encrypt(code_verifier),
                    redirect_uri,
                    expires_at,
                    now,
                    now,
                ),
            )
        for app_id in apps:
            self.set_app_status(app_id, "connecting")
        session = self.load_oauth_session(session_id)
        if session is None:
            raise GoogleIntegrationError(
                "OAuth session could not be saved",
                code="session_unavailable",
            )
        return session

    def load_oauth_session(self, session_id: str) -> PendingOAuthSession | None:
        with db.get_db_connection(self.db_path) as connection:
            row = connection.execute(
                """
                SELECT session_id, apps_json, scopes_json, client_profile,
                       client_id, state_hash, encrypted_code_verifier,
                       redirect_uri, status, expires_at, last_error
                FROM integration_auth_sessions
                WHERE session_id = ? AND owner_id = ? AND provider = ?
                """,
                (session_id, self.owner_id, GOOGLE_PROVIDER),
            ).fetchone()
        if row is None:
            return None
        try:
            apps = tuple(json.loads(row[1]))
            scopes = tuple(json.loads(row[2]))
        except (TypeError, json.JSONDecodeError):
            apps = ()
            scopes = ()
        encrypted_verifier = row[6]
        verifier = (
            self.cipher.decrypt(encrypted_verifier)
            if isinstance(encrypted_verifier, str) and encrypted_verifier
            else ""
        )
        return PendingOAuthSession(
            session_id=row[0],
            apps=apps,
            scopes=scopes,
            client_profile=row[3],
            client_id=row[4],
            state_hash=row[5],
            code_verifier=verifier,
            redirect_uri=row[7],
            status=row[8],
            expires_at=int(row[9]),
            last_error=row[10],
        )

    def begin_oauth_completion(
        self,
        session_id: str,
        *,
        state: str,
    ) -> PendingOAuthSession:
        session = self.load_oauth_session(session_id)
        if session is None:
            raise GoogleIntegrationError(
                "The OAuth session does not belong to this AIOS computer",
                code="invalid_session",
            )
        if session.expires_at <= int(time.time()):
            self.close_oauth_session(
                session_id,
                status="expired",
                error="OAuth session expired",
            )
            raise GoogleIntegrationError(
                "The OAuth session expired",
                code="expired_session",
            )
        if session.status != "pending":
            raise GoogleIntegrationError(
                f"The OAuth session is {session.status}",
                code="invalid_session",
            )
        if not hmac.compare_digest(session.state_hash, hash_oauth_state(state)):
            raise GoogleIntegrationError(
                "Google returned an invalid OAuth state",
                code="invalid_state",
            )
        with db.get_db_connection(self.db_path) as connection:
            result = connection.execute(
                """
                UPDATE integration_auth_sessions
                SET status = 'exchanging', updated_at = ?, last_error = NULL
                WHERE session_id = ? AND owner_id = ? AND provider = ?
                  AND status = 'pending'
                """,
                (
                    int(time.time()),
                    session_id,
                    self.owner_id,
                    GOOGLE_PROVIDER,
                ),
            )
        if result.rowcount != 1:
            raise GoogleIntegrationError(
                "The OAuth session is already being completed",
                code="invalid_session",
            )
        return session

    def set_oauth_session_status(
        self,
        session_id: str,
        *,
        status: str,
        error: str | None = None,
        clear_secrets: bool = False,
    ) -> None:
        secret_clause = (
            ", state_hash = '', encrypted_code_verifier = ''" if clear_secrets else ""
        )
        with db.get_db_connection(self.db_path) as connection:
            connection.execute(
                f"""
                UPDATE integration_auth_sessions
                SET status = ?, last_error = ?, updated_at = ?{secret_clause}
                WHERE session_id = ? AND owner_id = ? AND provider = ?
                """,
                (
                    status,
                    error,
                    int(time.time()),
                    session_id,
                    self.owner_id,
                    GOOGLE_PROVIDER,
                ),
            )

    def close_oauth_session(
        self,
        session_id: str,
        *,
        status: str,
        error: str | None = None,
    ) -> None:
        session = self.load_oauth_session(session_id)
        if session is None:
            return
        self.set_oauth_session_status(
            session_id,
            status=status,
            error=error,
            clear_secrets=True,
        )
        self._restore_app_statuses(session.apps, error=error)

    def expire_oauth_sessions(self) -> None:
        now = int(time.time())
        with db.get_db_connection(self.db_path) as connection:
            expired_rows = connection.execute(
                """
                SELECT apps_json
                FROM integration_auth_sessions
                WHERE owner_id = ? AND provider = ?
                  AND status IN ('pending', 'exchanging') AND expires_at <= ?
                """,
                (self.owner_id, GOOGLE_PROVIDER, now),
            ).fetchall()
            connection.execute(
                """
                UPDATE integration_auth_sessions
                SET status = 'expired', updated_at = ?,
                    state_hash = '', encrypted_code_verifier = '',
                    last_error = 'OAuth session expired'
                WHERE owner_id = ? AND provider = ?
                  AND status IN ('pending', 'exchanging') AND expires_at <= ?
                """,
                (now, self.owner_id, GOOGLE_PROVIDER, now),
            )
            active_rows = connection.execute(
                """
                SELECT apps_json
                FROM integration_auth_sessions
                WHERE owner_id = ? AND provider = ?
                  AND status IN ('pending', 'exchanging') AND expires_at > ?
                """,
                (self.owner_id, GOOGLE_PROVIDER, now),
            ).fetchall()
        expired_apps = _apps_from_rows(expired_rows)
        active_apps = _apps_from_rows(active_rows)
        self._restore_app_statuses(
            tuple(expired_apps.difference(active_apps)),
            error="OAuth session expired",
        )

    def _restore_app_statuses(
        self,
        apps: Iterable[str],
        *,
        error: str | None,
    ) -> None:
        connection = self.load_connection()
        scopes = connection.scopes if connection else ()
        connected_apps: set[str] = set()
        if any(scope in scopes for scope in GMAIL_SCOPES):
            connected_apps.add("gmail")
        if any(scope in scopes for scope in CALENDAR_SCOPES):
            connected_apps.add("google-calendar")
        for app_id in apps:
            if app_id in connected_apps:
                self.set_app_status(
                    app_id,
                    "connected",
                    connection_id=connection.connection_id if connection else None,
                )
            else:
                self.set_app_status(
                    app_id,
                    "error" if error else "available",
                    error=error,
                )

    def set_app_status(
        self,
        app_id: str,
        status: str,
        *,
        connection_id: str | None = None,
        error: str | None = None,
    ) -> None:
        now = int(time.time())
        with db.get_db_connection(self.db_path) as connection:
            connection.execute(
                """
                INSERT INTO integration_apps (
                    owner_id, app_id, provider, connection_id, status,
                    created_at, updated_at, last_error
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(owner_id, app_id) DO UPDATE SET
                    provider = excluded.provider,
                    connection_id = excluded.connection_id,
                    status = excluded.status,
                    updated_at = excluded.updated_at,
                    last_error = excluded.last_error
                """,
                (
                    self.owner_id,
                    app_id,
                    GOOGLE_PROVIDER,
                    connection_id,
                    status,
                    now,
                    now,
                    error,
                ),
            )

    def app_statuses(self) -> dict[str, str]:
        self.expire_oauth_sessions()
        with db.get_db_connection(self.db_path) as connection:
            rows = connection.execute(
                """
                SELECT app_id, status
                FROM integration_apps
                WHERE owner_id = ? AND provider = ?
                """,
                (self.owner_id, GOOGLE_PROVIDER),
            ).fetchall()
        return {row[0]: row[1] for row in rows}


HttpClientFactory = Callable[[], httpx.AsyncClient]


class GoogleTokenClient:
    """Exchange, refresh, and revoke Google tokens directly from mini-AIOS."""

    def __init__(
        self,
        config: GoogleConfig,
        *,
        http_client_factory: HttpClientFactory | None = None,
    ) -> None:
        self.config = config
        self._http_client_factory = http_client_factory or (
            lambda: httpx.AsyncClient(timeout=15.0)
        )
        self._oauth = PublicOAuthTokenClient(
            config.public_provider,
            http_client_factory=self._http_client_factory,
        )

    async def exchange_code(
        self,
        *,
        code: str,
        code_verifier: str,
        redirect_uri: str,
    ) -> dict[str, Any]:
        try:
            return await self._oauth.exchange_code(
                code=code,
                code_verifier=code_verifier,
                redirect_uri=redirect_uri,
            )
        except OAuthProtocolError as exc:
            raise GoogleIntegrationError(str(exc), code=exc.code) from exc

    async def refresh(
        self,
        refresh_token: str,
        *,
        client_id: str | None = None,
    ) -> dict[str, Any]:
        oauth = self._oauth
        if client_id and client_id != self.config.client_id:
            provider = self.config.public_provider
            oauth = PublicOAuthTokenClient(
                PublicOAuthProvider(
                    id=provider.id,
                    client_profile=provider.client_profile,
                    client_id=client_id,
                    authorization_endpoint=provider.authorization_endpoint,
                    token_endpoint=provider.token_endpoint,
                    redirect_uri=provider.redirect_uri,
                    revocation_endpoint=provider.revocation_endpoint,
                    authorization_parameters=provider.authorization_parameters,
                ),
                http_client_factory=self._http_client_factory,
            )
        try:
            return await oauth.refresh(refresh_token)
        except OAuthProtocolError as exc:
            raise GoogleIntegrationError(str(exc), code=exc.code) from exc

    async def revoke(self, token: str) -> None:
        try:
            await self._oauth.revoke(token)
        except OAuthProtocolError as exc:
            raise GoogleIntegrationError(str(exc), code=exc.code) from exc

    async def account_info(self, access_token: str) -> dict[str, Any]:
        try:
            async with self._http_client_factory() as client:
                response = await client.get(
                    GOOGLE_USERINFO_URL,
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Accept": "application/json",
                        "Cache-Control": "no-store",
                    },
                )
        except httpx.HTTPError as exc:
            raise GoogleIntegrationError(
                "Could not load the connected Google account",
                code="oauth_unavailable",
            ) from exc
        if response.is_error:
            raise GoogleIntegrationError(
                "Google rejected the new access token",
                code="invalid_oauth_response",
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise GoogleIntegrationError(
                "Google OAuth returned an invalid response",
                code="invalid_oauth_response",
            ) from exc
        if not isinstance(payload, dict):
            raise GoogleIntegrationError(
                "Google OAuth returned an invalid response",
                code="invalid_oauth_response",
            )
        return payload


class GoogleOAuthService:
    def __init__(
        self,
        *,
        owner_id: str | None = None,
        config: GoogleConfig | None = None,
        store: GoogleCredentialStore | None = None,
        token_client: GoogleTokenClient | None = None,
        http_client_factory: HttpClientFactory | None = None,
    ) -> None:
        self.owner_id = owner_id or _current_owner_id()
        self.config = config or GoogleConfig.from_env()
        self.store = store or GoogleCredentialStore(owner_id=self.owner_id)
        self._http_client_factory = http_client_factory or (
            lambda: httpx.AsyncClient(timeout=15.0)
        )
        self.token_client = token_client or GoogleTokenClient(
            self.config,
            http_client_factory=self._http_client_factory,
        )

    def connection_status(self) -> dict[str, Any]:
        connection = self.store.load_connection()
        scopes = connection.scopes if connection else ()
        app_statuses = self.store.app_statuses()
        return {
            "provider": GOOGLE_PROVIDER,
            "oauthFlowVersion": 2,
            "clientProfile": self.config.client_profile,
            "enabled": self.config.enabled,
            "configured": self.config.configured,
            "connected": connection is not None and connection.status == "connected",
            "status": connection.status if connection else "disconnected",
            "connectionId": connection.connection_id if connection else None,
            "accountEmail": connection.account_email if connection else None,
            "scopes": list(scopes),
            "services": {
                "gmail": all(scope in scopes for scope in GMAIL_SCOPES),
                "calendar": any(scope in scopes for scope in CALENDAR_SCOPES),
            },
            "apps": {
                "gmail": app_statuses.get("gmail", "available"),
                "google-calendar": app_statuses.get(
                    "google-calendar",
                    "available",
                ),
            },
            "lastRefreshedAt": (connection.last_refreshed_at if connection else None),
            "lastError": connection.last_error if connection else None,
        }

    async def start_authorization(
        self,
        *,
        services: Iterable[str] = ("gmail", "calendar"),
        client_profile: str | None = None,
    ) -> dict[str, Any]:
        self._require_configuration()
        if client_profile and client_profile != self.config.client_profile:
            raise GoogleIntegrationError(
                "This Google OAuth client profile is not available",
                code="unsupported_client_profile",
            )
        normalized_services = tuple(
            dict.fromkeys(service.strip().lower() for service in services)
        )
        scopes = scopes_for_services(
            normalized_services,
            allowed_scopes=self.config.scopes,
        )
        apps = tuple(SERVICE_APP_IDS[service] for service in normalized_services)
        pkce = create_pkce_material()
        state = create_oauth_state()
        session_id = secrets.token_urlsafe(24)
        expires_at = int(time.time()) + _AUTHORIZATION_TTL_SECONDS
        provider = self.config.public_provider
        authorization_url = build_authorization_url(
            provider,
            scopes=scopes,
            state=state,
            code_challenge=pkce.challenge,
        )
        self.store.save_oauth_session(
            session_id=session_id,
            apps=apps,
            scopes=scopes,
            client_profile=provider.client_profile,
            client_id=provider.client_id,
            state_hash=hash_oauth_state(state),
            code_verifier=pkce.verifier,
            redirect_uri=provider.redirect_uri,
            expires_at=expires_at,
        )
        return {
            "sessionId": session_id,
            "authorizationUrl": authorization_url,
            "redirectUrl": provider.redirect_uri,
            "expiresAt": expires_at,
            "oauthFlowVersion": 2,
            "clientProfile": provider.client_profile,
        }

    async def complete_authorization(
        self,
        *,
        session_id: str,
        code: str,
        state: str,
    ) -> GoogleTokens:
        self._require_configuration()
        if not code or not state:
            raise GoogleIntegrationError(
                "Google did not return an authorization code and state",
                code="invalid_callback",
            )
        pending = self.store.begin_oauth_completion(session_id, state=state)
        if (
            pending.client_profile != self.config.client_profile
            or pending.client_id != self.config.client_id
            or pending.redirect_uri != self.config.redirect_uri
        ):
            self.store.close_oauth_session(
                session_id,
                status="failed",
                error="OAuth client configuration changed",
            )
            raise GoogleIntegrationError(
                "Google OAuth configuration changed; start the connection again",
                code="client_configuration_changed",
            )
        try:
            payload = await self.token_client.exchange_code(
                code=code,
                code_verifier=pending.code_verifier,
                redirect_uri=pending.redirect_uri,
            )
        except GoogleIntegrationError as exc:
            if exc.code == "oauth_unavailable":
                self.store.set_oauth_session_status(
                    session_id,
                    status="pending",
                    error=str(exc),
                )
            else:
                self.store.close_oauth_session(
                    session_id,
                    status="failed",
                    error=str(exc),
                )
            raise
        tokens = self._tokens_from_payload(
            payload,
            fallback_scopes=pending.scopes,
        )
        account: dict[str, Any] = {}
        try:
            account = await self.token_client.account_info(tokens.access_token)
        except GoogleIntegrationError:
            # Account labels are optional; the durable grant is more important
            # than failing the connection after Google issued the tokens.
            pass
        raw_account_id = account.get("sub")
        raw_account_email = account.get("email")
        tokens = GoogleTokens(
            access_token=tokens.access_token,
            refresh_token=tokens.refresh_token,
            token_type=tokens.token_type,
            scopes=tokens.scopes,
            expires_at=tokens.expires_at,
            provider_account_id=(
                raw_account_id if isinstance(raw_account_id, str) else None
            ),
            account_email=(
                raw_account_email if isinstance(raw_account_email, str) else None
            ),
        )
        try:
            self.store.save_tokens(
                tokens,
                oauth_client_profile=pending.client_profile,
                oauth_client_id=pending.client_id,
                authorized_at=int(time.time()),
            )
        except GoogleIntegrationError as exc:
            self.store.close_oauth_session(
                session_id,
                status="failed",
                error=str(exc),
            )
            raise
        _access_cache[(self.owner_id, GOOGLE_PROVIDER)] = AccessToken(
            tokens.access_token,
            tokens.expires_at,
        )
        self.store.set_oauth_session_status(
            session_id,
            status="completed",
            clear_secrets=True,
        )
        return tokens

    def cancel_authorization(self, *, session_id: str) -> None:
        self.store.close_oauth_session(
            session_id,
            status="cancelled",
        )

    async def valid_access_token(
        self,
        *,
        required_scopes: Iterable[str] = (),
        rejected_token: str | None = None,
    ) -> str:
        connection = self.store.load_connection()
        if connection is None:
            raise GoogleIntegrationError(
                "Google is not connected",
                code="not_connected",
            )
        if connection.status == "reauth_required":
            raise GoogleIntegrationError(
                "Google needs to be reconnected",
                code="reauth_required",
            )
        missing = set(required_scopes).difference(connection.scopes)
        if missing:
            raise GoogleIntegrationError(
                "Google authorization is missing required scopes",
                code="missing_scopes",
            )
        cache_key = (self.owner_id, GOOGLE_PROVIDER)
        cached = _access_cache.get(cache_key)
        if cached is not None and cached.usable and cached.value != rejected_token:
            return cached.value

        lock = _refresh_locks.setdefault(cache_key, asyncio.Lock())
        async with lock:
            cached = _access_cache.get(cache_key)
            if cached is not None and cached.usable and cached.value != rejected_token:
                return cached.value
            try:
                payload = await self.token_client.refresh(
                    connection.refresh_token,
                    client_id=connection.oauth_client_id,
                )
            except GoogleIntegrationError as exc:
                if exc.code in {
                    "invalid_grant",
                    "invalid_client",
                    "unauthorized_client",
                }:
                    self.store.set_status(
                        "reauth_required",
                        error="Google authorization was revoked or expired",
                    )
                raise
            refreshed = self._tokens_from_payload(
                payload,
                fallback_refresh_token=connection.refresh_token,
                fallback_scopes=connection.scopes,
            )
            self.store.save_tokens(refreshed)
            _access_cache[cache_key] = AccessToken(
                refreshed.access_token,
                refreshed.expires_at,
            )
            return refreshed.access_token

    async def disconnect(self, *, revoke: bool = True) -> None:
        connection = self.store.load_connection()
        try:
            if revoke and connection is not None:
                try:
                    await self.token_client.revoke(connection.refresh_token)
                except GoogleIntegrationError:
                    pass
        finally:
            self.store.delete_connection()

    def _tokens_from_payload(
        self,
        payload: dict[str, Any],
        *,
        fallback_refresh_token: str | None = None,
        fallback_scopes: tuple[str, ...] | None = None,
    ) -> GoogleTokens:
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise GoogleIntegrationError(
                "Google OAuth response did not include an access token",
                code="missing_access_token",
            )
        refresh_token = payload.get("refresh_token", fallback_refresh_token)
        if not isinstance(refresh_token, str):
            refresh_token = None
        token_type = payload.get("token_type", "Bearer")
        if not isinstance(token_type, str):
            token_type = "Bearer"
        raw_scope = payload.get("scope")
        scopes = (
            tuple(raw_scope.split())
            if isinstance(raw_scope, str)
            else fallback_scopes or self.config.scopes
        )
        try:
            expires_in = max(0, int(payload.get("expires_in", 3600)))
        except (TypeError, ValueError):
            expires_in = 3600
        return GoogleTokens(
            access_token=access_token,
            refresh_token=refresh_token,
            token_type=token_type,
            scopes=scopes,
            expires_at=int(time.time()) + expires_in,
        )

    def _require_configuration(self) -> None:
        if not self.config.configured:
            raise GoogleIntegrationError(
                "Google OAuth public client is not configured on this computer",
                code="not_configured",
            )


def _current_owner_id() -> str:
    link = db.get_device_link(db.DB_PATH)
    if link is None:
        return "local"
    owner_id = link.get("owner_user_id")
    return owner_id if isinstance(owner_id, str) and owner_id else "local"
