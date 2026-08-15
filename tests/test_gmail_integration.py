from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from agno.tools.mcp import MCPTools
from cryptography.fernet import Fernet

from aios_core import db
from aios_core.integrations import google as google_module
from aios_core.integrations.google import (
    CALENDAR_SCOPES,
    GMAIL_SCOPES,
    CredentialCipher,
    GoogleConfig,
    GoogleCredentialStore,
    GoogleIntegrationError,
    GoogleOAuthService,
    GoogleTokens,
)
from aios_core.integrations.google_mcp import (
    GoogleMCPTools,
    LocalGmailMCPTools,
    get_google_mcp_toolkits,
)
from aios_core.integrations.oauth import hash_oauth_state


@pytest.fixture(autouse=True)
def clear_google_runtime_cache():
    google_module._access_cache.clear()
    google_module._refresh_locks.clear()
    yield
    google_module._access_cache.clear()
    google_module._refresh_locks.clear()


@pytest.fixture
def google_config() -> GoogleConfig:
    return GoogleConfig(
        client_id="ios-public-client.apps.googleusercontent.com",
        redirect_uri=("com.googleusercontent.apps.ios-public-client:/oauth2redirect"),
    )


@pytest.fixture
def credential_store(tmp_path) -> GoogleCredentialStore:
    return GoogleCredentialStore(
        owner_id="owner-a",
        db_path=str(tmp_path / "aios.db"),
        cipher=CredentialCipher(Fernet.generate_key()),
    )


def _tokens(
    *,
    access_token: str = "access-secret",
    refresh_token: str = "refresh-secret",
    scopes: tuple[str, ...] = (*GMAIL_SCOPES, *CALENDAR_SCOPES),
) -> GoogleTokens:
    return GoogleTokens(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="Bearer",
        scopes=scopes,
        expires_at=int(time.time()) + 3600,
    )


def test_only_refresh_token_is_encrypted_at_rest(
    credential_store: GoogleCredentialStore,
) -> None:
    credential_store.save_tokens(_tokens())

    loaded = credential_store.load_connection()
    assert loaded is not None
    assert loaded.refresh_token == "refresh-secret"

    with sqlite3.connect(credential_store.db_path) as connection:
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(integration_connections)")
        }
        stored = connection.execute(
            """
            SELECT encrypted_refresh_token
            FROM integration_connections
            WHERE owner_id = 'owner-a'
            """
        ).fetchone()
    assert "access_token" not in columns
    assert stored is not None
    assert "refresh-secret" not in stored[0]


def test_mini_aios_creates_pkce_session_and_local_app_records(
    credential_store: GoogleCredentialStore,
    google_config: GoogleConfig,
) -> None:
    service = GoogleOAuthService(
        owner_id="owner-a",
        config=google_config,
        store=credential_store,
    )
    start = asyncio.run(service.start_authorization(services=("gmail",)))
    parameters = parse_qs(urlparse(start["authorizationUrl"]).query)

    assert start["authorizationUrl"].startswith("https://accounts.google.com/")
    assert start["redirectUrl"] == google_config.redirect_uri
    assert start["oauthFlowVersion"] == 2
    assert parameters["client_id"] == [google_config.client_id]
    assert parameters["code_challenge_method"] == ["S256"]
    assert parameters["access_type"] == ["offline"]
    assert "https://www.googleapis.com/auth/gmail.readonly" in parameters["scope"][0]
    assert "https://www.googleapis.com/auth/gmail.send" in parameters["scope"][0]
    assert "code_challenge" in parameters
    assert "state" in parameters

    session = credential_store.load_oauth_session(start["sessionId"])
    assert session is not None
    assert session.apps == ("gmail",)
    assert session.state_hash == hash_oauth_state(parameters["state"][0])
    assert session.code_verifier
    with sqlite3.connect(credential_store.db_path) as connection:
        stored = connection.execute(
            """
            SELECT state_hash, encrypted_code_verifier
            FROM integration_auth_sessions
            WHERE session_id = ?
            """,
            (start["sessionId"],),
        ).fetchone()
    assert stored is not None
    assert parameters["state"][0] not in stored
    assert session.code_verifier not in stored
    assert credential_store.app_statuses() == {"gmail": "connecting"}


def test_phone_callback_is_exchanged_directly_and_saved_locally(
    credential_store: GoogleCredentialStore,
    google_config: GoogleConfig,
) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url == httpx.URL(google_config.token_endpoint):
            form = parse_qs(request.content.decode())
            assert form["client_id"] == [google_config.client_id]
            assert form["code"] == ["authorization-code"]
            assert form["grant_type"] == ["authorization_code"]
            assert form["redirect_uri"] == [google_config.redirect_uri]
            assert form["code_verifier"][0]
            assert "client_secret" not in form
            return httpx.Response(
                200,
                json={
                    "access_token": "access",
                    "refresh_token": "refresh",
                    "expires_in": 3600,
                    "scope": (
                        "openid email "
                        "https://www.googleapis.com/auth/gmail.readonly "
                        "https://www.googleapis.com/auth/gmail.send"
                    ),
                    "token_type": "Bearer",
                },
            )
        assert request.url == httpx.URL(google_module.GOOGLE_USERINFO_URL)
        assert request.headers["authorization"] == "Bearer access"
        return httpx.Response(
            200,
            json={
                "sub": "google-account-id",
                "email": "user@example.com",
            },
        )

    service = GoogleOAuthService(
        owner_id="owner-a",
        config=google_config,
        store=credential_store,
        http_client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )
    start = asyncio.run(service.start_authorization(services=("gmail",)))
    state = parse_qs(urlparse(start["authorizationUrl"]).query)["state"][0]
    asyncio.run(
        service.complete_authorization(
            session_id=start["sessionId"],
            code="authorization-code",
            state=state,
        )
    )

    connection = credential_store.load_connection()
    assert connection is not None
    assert connection.refresh_token == "refresh"
    assert connection.account_email == "user@example.com"
    assert connection.oauth_client_id == google_config.client_id
    assert connection.oauth_client_profile == google_config.client_profile
    session = credential_store.load_oauth_session(start["sessionId"])
    assert session is not None
    assert session.status == "completed"
    assert session.code_verifier == ""
    assert session.state_hash == ""
    assert credential_store.app_statuses() == {"gmail": "connected"}
    assert len(requests) == 2
    with pytest.raises(GoogleIntegrationError, match="completed"):
        asyncio.run(
            service.complete_authorization(
                session_id=start["sessionId"],
                code="authorization-code",
                state=state,
            )
        )
    assert len(requests) == 2


def test_restart_refreshes_directly_from_local_refresh_token(
    credential_store: GoogleCredentialStore,
    google_config: GoogleConfig,
) -> None:
    credential_store.save_tokens(
        _tokens(),
        oauth_client_profile=google_config.client_profile,
        oauth_client_id=google_config.client_id,
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL(google_config.token_endpoint)
        form = parse_qs(request.content.decode())
        assert form == {
            "client_id": [google_config.client_id],
            "grant_type": ["refresh_token"],
            "refresh_token": ["refresh-secret"],
        }
        return httpx.Response(
            200,
            json={
                "access_token": "new-access",
                "expires_in": 3600,
                "token_type": "Bearer",
            },
        )

    service = GoogleOAuthService(
        owner_id="owner-a",
        config=google_config,
        store=credential_store,
        http_client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )

    assert asyncio.run(service.valid_access_token()) == "new-access"
    refreshed = credential_store.load_connection()
    assert refreshed is not None
    assert refreshed.refresh_token == "refresh-secret"
    assert refreshed.status == "connected"


def test_unknown_session_and_invalid_state_are_rejected(
    credential_store: GoogleCredentialStore,
    google_config: GoogleConfig,
) -> None:
    service = GoogleOAuthService(
        owner_id="owner-a",
        config=google_config,
        store=credential_store,
    )
    with pytest.raises(GoogleIntegrationError, match="does not belong"):
        asyncio.run(
            service.complete_authorization(
                session_id="unknown-session",
                code="code",
                state="state-" + "x" * 20,
            )
        )
    start = asyncio.run(service.start_authorization(services=("gmail",)))
    with pytest.raises(GoogleIntegrationError, match="invalid OAuth state"):
        asyncio.run(
            service.complete_authorization(
                session_id=start["sessionId"],
                code="code",
                state="wrong-" + "x" * 20,
            )
        )
    assert credential_store.load_oauth_session(start["sessionId"]).status == "pending"


def test_cancelled_and_expired_sessions_do_not_stay_connecting(
    credential_store: GoogleCredentialStore,
    google_config: GoogleConfig,
) -> None:
    cancelled_id = "cancelled-" + "x" * 20
    credential_store.save_oauth_session(
        session_id=cancelled_id,
        apps=("gmail",),
        scopes=GMAIL_SCOPES,
        client_profile=google_config.client_profile,
        client_id=google_config.client_id,
        state_hash=hash_oauth_state("state-" + "x" * 20),
        code_verifier="verifier-" + "x" * 50,
        redirect_uri=google_config.redirect_uri,
        expires_at=int(time.time()) + 600,
    )
    service = GoogleOAuthService(
        owner_id="owner-a",
        config=google_config,
        store=credential_store,
    )
    service.cancel_authorization(session_id=cancelled_id)

    assert credential_store.load_oauth_session(cancelled_id).status == "cancelled"
    assert credential_store.load_oauth_session(cancelled_id).code_verifier == ""
    assert credential_store.app_statuses()["gmail"] == "available"

    expired_id = "expired-" + "x" * 20
    credential_store.save_oauth_session(
        session_id=expired_id,
        apps=("google-calendar",),
        scopes=CALENDAR_SCOPES,
        client_profile=google_config.client_profile,
        client_id=google_config.client_id,
        state_hash=hash_oauth_state("state-" + "y" * 20),
        code_verifier="verifier-" + "y" * 50,
        redirect_uri=google_config.redirect_uri,
        expires_at=int(time.time()) - 1,
    )

    assert credential_store.app_statuses()["google-calendar"] == "error"
    assert credential_store.load_oauth_session(expired_id).status == "expired"


def test_credentials_are_isolated_by_owner(tmp_path) -> None:
    database = str(tmp_path / "aios.db")
    cipher = CredentialCipher(Fernet.generate_key())
    owner_a = GoogleCredentialStore(
        owner_id="owner-a",
        db_path=database,
        cipher=cipher,
    )
    owner_b = GoogleCredentialStore(
        owner_id="owner-b",
        db_path=database,
        cipher=cipher,
    )
    owner_a.save_tokens(_tokens())

    assert owner_a.load_connection() is not None
    assert owner_b.load_connection() is None
    assert owner_a.app_statuses()["gmail"] == "connected"
    assert owner_b.app_statuses() == {}


def test_legacy_gmail_row_migrates_without_copying_access_token(tmp_path) -> None:
    database = str(tmp_path / "aios.db")
    cipher = CredentialCipher(Fernet.generate_key())
    db.initialize_app_db(database)
    with db.get_db_connection(database) as connection:
        connection.executescript(
            """
            CREATE TABLE integration_credentials (
                provider TEXT PRIMARY KEY,
                access_token TEXT NOT NULL,
                refresh_token TEXT,
                token_type TEXT NOT NULL,
                scopes_json TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            """
        )
        connection.execute(
            """
            INSERT INTO integration_credentials
            VALUES ('gmail', ?, ?, 'Bearer', ?, 1, 1, 1)
            """,
            (
                cipher.encrypt("legacy-access"),
                cipher.encrypt("legacy-refresh"),
                json.dumps(GMAIL_SCOPES),
            ),
        )

    store = GoogleCredentialStore(
        owner_id="owner-a",
        db_path=database,
        cipher=cipher,
    )
    migrated = store.load_connection()

    assert migrated is not None
    assert migrated.refresh_token == "legacy-refresh"
    with sqlite3.connect(database) as connection:
        old_count = connection.execute(
            "SELECT COUNT(*) FROM integration_credentials"
        ).fetchone()[0]
        new_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(integration_connections)")
        }
    assert old_count == 0
    assert "access_token" not in new_columns


def test_mcp_toolkits_match_granted_google_services(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "aios.db"))
    monkeypatch.setenv(
        "AIOS_CREDENTIAL_ENCRYPTION_KEY",
        Fernet.generate_key().decode(),
    )
    monkeypatch.setenv(
        "AIOS_GOOGLE_OAUTH_CLIENT_ID",
        "ios-public-client.apps.googleusercontent.com",
    )
    monkeypatch.setenv(
        "AIOS_GOOGLE_OAUTH_REDIRECT_URI",
        "com.googleusercontent.apps.ios-public-client:/oauth2redirect",
    )
    db.save_device_link(
        db_path=db.DB_PATH,
        device_token="paired-device-token",
        local_token="local-token",
        owner_user_id="local",
        owner_email="user@example.com",
        slug="device",
        paired_at=int(time.time()),
    )

    assert get_google_mcp_toolkits() == []

    GoogleCredentialStore().save_tokens(_tokens())
    toolkits = get_google_mcp_toolkits()

    assert [toolkit.tool_name_prefix for toolkit in toolkits] == [
        "gmail",
        "calendar",
    ]
    assert isinstance(toolkits[0], LocalGmailMCPTools)
    assert toolkits[0].include_tools == [
        "search_messages",
        "get_message",
        "get_thread",
        "list_labels",
        "send_email",
    ]
    assert toolkits[0].requires_confirmation_tools == []
    assert toolkits[1].requires_confirmation_tools == [
        "create_event",
        "update_event",
        "respond_to_event",
    ]


def test_mcp_connection_receives_current_bearer_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StubOAuthService:
        async def valid_access_token(self, *, required_scopes=()) -> str:
            assert required_scopes == GMAIL_SCOPES
            return "current-access-token"

    async def fake_connect(toolkit, force=False):
        return {"connected": True, "force": force}

    monkeypatch.setattr(MCPTools, "connect", fake_connect)
    toolkit = GoogleMCPTools(
        oauth_service=StubOAuthService(),
        service_name="gmail",
        url="https://gmail.example/mcp",
        tools=("search_threads",),
        required_scopes=GMAIL_SCOPES,
    )

    result = asyncio.run(toolkit.connect(force=True))

    assert result == {"connected": True, "force": True}
    assert toolkit.server_params is not None
    assert toolkit.server_params.headers == {
        "Authorization": "Bearer current-access-token",
        "Cache-Control": "no-store",
    }
