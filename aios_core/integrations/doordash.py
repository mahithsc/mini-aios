from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

from .. import db
from ..mcp_servers.doordash.client import (
    DoorDashCLIClient,
    DoorDashCLIError,
    resolve_dd_cli_executable,
)

DOORDASH_PROVIDER = "doordash"
DEFAULT_DOORDASH_TOOLS = (
    "run_cli",
)
_TRUTHY = {"1", "true", "yes", "on"}


class DoorDashIntegrationError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        status_code: int = 502,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class DoorDashConfig:
    enabled: bool = True
    executable: str | None = None
    tools: tuple[str, ...] = DEFAULT_DOORDASH_TOOLS

    @property
    def configured(self) -> bool:
        return bool(self.executable)

    @classmethod
    def from_env(cls) -> DoorDashConfig:
        enabled_value = os.getenv("AIOS_DOORDASH_ENABLED")
        enabled = (
            True
            if enabled_value is None
            else enabled_value.strip().lower() in _TRUTHY
        )
        configured_path = os.getenv("AIOS_DOORDASH_CLI_PATH")
        executable = resolve_dd_cli_executable(configured_path)
        raw_tools = os.getenv("AIOS_DOORDASH_TOOLS")
        tools = (
            tuple(
                tool.strip()
                for tool in raw_tools.split(",")
                if tool.strip() in DEFAULT_DOORDASH_TOOLS
            )
            if raw_tools is not None
            else DEFAULT_DOORDASH_TOOLS
        )
        return cls(enabled=enabled, executable=executable, tools=tools)


@dataclass(frozen=True)
class StoredDoorDashConnection:
    status: str
    connected_at: int | None
    updated_at: int
    last_error: str | None


class DoorDashConnectionStore:
    """Stores integration state only; dd-cli owns the actual credentials."""

    def __init__(
        self,
        *,
        owner_id: str = "local",
        db_path: str | None = None,
    ) -> None:
        self.owner_id = owner_id
        self.db_path = db_path or db.DB_PATH
        self._initialize()

    def _initialize(self) -> None:
        db.initialize_app_db(self.db_path)
        with db.get_db_connection(self.db_path) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS integration_cli_connections (
                    owner_id      TEXT NOT NULL,
                    provider      TEXT NOT NULL,
                    status        TEXT NOT NULL,
                    connected_at  INTEGER,
                    updated_at    INTEGER NOT NULL,
                    last_error    TEXT,
                    PRIMARY KEY(owner_id, provider)
                );

                CREATE INDEX IF NOT EXISTS idx_integration_cli_connections
                    ON integration_cli_connections(owner_id, provider);
                """
            )

    def load(self) -> StoredDoorDashConnection | None:
        with db.get_db_connection(self.db_path) as connection:
            row = connection.execute(
                """
                SELECT status, connected_at, updated_at, last_error
                FROM integration_cli_connections
                WHERE owner_id = ? AND provider = ?
                """,
                (self.owner_id, DOORDASH_PROVIDER),
            ).fetchone()
        if row is None:
            return None
        return StoredDoorDashConnection(
            status=row[0],
            connected_at=row[1],
            updated_at=row[2],
            last_error=row[3],
        )

    def set_status(
        self,
        status: str,
        *,
        error: str | None = None,
    ) -> None:
        now = int(time.time())
        existing = self.load()
        connected_at = (
            now
            if status == "connected"
            else existing.connected_at if existing is not None else None
        )
        with db.get_db_connection(self.db_path) as connection:
            connection.execute(
                """
                INSERT INTO integration_cli_connections (
                    owner_id, provider, status, connected_at, updated_at,
                    last_error
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(owner_id, provider) DO UPDATE SET
                    status = excluded.status,
                    connected_at = excluded.connected_at,
                    updated_at = excluded.updated_at,
                    last_error = excluded.last_error
                """,
                (
                    self.owner_id,
                    DOORDASH_PROVIDER,
                    status,
                    connected_at,
                    now,
                    error,
                ),
            )


class DoorDashConnectionService:
    """Coordinates dd-cli's browser login and mini-AIOS integration state."""

    def __init__(
        self,
        *,
        owner_id: str | None = None,
        config: DoorDashConfig | None = None,
        store: DoorDashConnectionStore | None = None,
        client: DoorDashCLIClient | None = None,
    ) -> None:
        self.owner_id = owner_id or _current_owner_id()
        self.config = config or DoorDashConfig.from_env()
        self.store = store or DoorDashConnectionStore(owner_id=self.owner_id)
        self.client = client or DoorDashCLIClient(
            executable=self.config.executable
        )

    def connection_status(self) -> dict[str, Any]:
        connection = self.store.load()
        installed = self.config.configured
        connected = (
            self.config.enabled
            and installed
            and connection is not None
            and connection.status == "connected"
        )
        if not self.config.enabled:
            status = "disabled"
        elif not installed:
            status = "not_installed"
        else:
            status = connection.status if connection is not None else "disconnected"
        return {
            "provider": DOORDASH_PROVIDER,
            "enabled": self.config.enabled,
            "configured": installed,
            "toolAvailable": self.config.enabled and installed,
            "connected": connected,
            "status": status,
            "credentialOwner": "dd-cli",
            "credentialDelivery": "operating-system-keychain",
            "executable": self.config.executable,
            "connectedAt": (
                connection.connected_at if connection is not None else None
            ),
            "lastError": connection.last_error if connection is not None else None,
        }

    async def connect(self) -> dict[str, Any]:
        if not self.config.enabled:
            raise DoorDashIntegrationError(
                "The DoorDash integration is disabled",
                code="disabled",
                status_code=503,
            )
        if not self.config.configured:
            raise DoorDashIntegrationError(
                "dd-cli is not installed or AIOS_DOORDASH_CLI_PATH is invalid",
                code="not_configured",
                status_code=503,
            )
        self.store.set_status("connecting")
        try:
            await self.client.login()
        except DoorDashCLIError as exc:
            self.store.set_status("error", error=str(exc))
            raise DoorDashIntegrationError(
                str(exc),
                code=exc.code,
                status_code=exc.status_code,
            ) from exc
        self.store.set_status("connected")
        return self.connection_status()

    async def disconnect(self) -> dict[str, Any]:
        self.store.set_status("disconnected")
        return {
            "disconnected": True,
            "credentialsRemoved": False,
            "message": (
                "DoorDash tools are disabled in mini-AIOS. dd-cli may still "
                "retain its login in the operating-system keychain."
            ),
        }


def _current_owner_id() -> str:
    link = db.get_device_link(db.DB_PATH)
    if link is None:
        return "local"
    owner_id = link.get("owner_user_id")
    return owner_id if isinstance(owner_id, str) and owner_id else "local"
