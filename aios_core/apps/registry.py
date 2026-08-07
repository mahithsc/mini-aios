from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path

from ..db import get_db_connection, initialize_app_db
from ..workspace import get_runtime_paths
from .manifest import (
    canonical_manifest_json,
    parse_manifest,
    validate_slug,
)
from .models import AppManifest, AppOrigin, AppRecord


class AppRegistryError(RuntimeError):
    """Base error for registry operations."""


class AppNotFoundError(AppRegistryError):
    pass


class AppConflictError(AppRegistryError):
    pass


class AppLifecycleError(AppRegistryError):
    pass


class AppRegistry:
    """SQLite metadata registry for editable and activated Apps.

    Filesystem validation and container execution deliberately live outside
    this class. Lifecycle writes are short transactions so an older active
    snapshot remains usable while a source update is being validated.
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = str(db_path or get_runtime_paths().database)
        Path(self.db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        initialize_app_db(self.db_path)

    def register(
        self,
        slug: str,
        *,
        origin: AppOrigin | str = AppOrigin.USER,
        name: str | None = None,
        description: str = "",
        version: str = "0.0.0",
        created_by_chat_id: str | None = None,
        created_by_run_id: str | None = None,
        app_id: str | None = None,
    ) -> AppRecord:
        slug = validate_slug(slug)
        try:
            normalized_origin = AppOrigin(origin)
        except ValueError as exc:
            raise AppRegistryError(f"unsupported App origin: {origin}") from exc
        now = int(time.time() * 1000)
        identifier = app_id or str(uuid.uuid4())
        root_path = f"applications/{slug}"
        try:
            with get_db_connection(self.db_path) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO apps (
                        id, slug, name, description, version, origin,
                        root_path, enabled, network_approved,
                        created_by_chat_id, created_by_run_id,
                        created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?, ?)
                    """,
                    (
                        identifier,
                        slug,
                        name or slug,
                        description,
                        version,
                        normalized_origin.value,
                        root_path,
                        created_by_chat_id,
                        created_by_run_id,
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise AppConflictError(
                f"an App named '{slug}' is already registered"
            ) from exc
        return self.require(identifier)

    def get(self, app_id_or_slug: str) -> AppRecord | None:
        with get_db_connection(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM apps WHERE id = ? OR slug = ?",
                (app_id_or_slug, app_id_or_slug),
            ).fetchone()
        return self._row_to_record(row) if row is not None else None

    def require(self, app_id_or_slug: str) -> AppRecord:
        app = self.get(app_id_or_slug)
        if app is None:
            raise AppNotFoundError(f"App not found: {app_id_or_slug}")
        return app

    def list(self, *, enabled: bool | None = None) -> list[AppRecord]:
        query = "SELECT * FROM apps"
        parameters: tuple[object, ...] = ()
        if enabled is not None:
            query += " WHERE enabled = ?"
            parameters = (int(enabled),)
        query += " ORDER BY slug"
        with get_db_connection(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(query, parameters).fetchall()
        return [self._row_to_record(row) for row in rows]

    def record_validation(
        self,
        app_id_or_slug: str,
        manifest: AppManifest,
        content_hash: str,
    ) -> AppRecord:
        app = self.require(app_id_or_slug)
        now = int(time.time() * 1000)
        with get_db_connection(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE apps
                SET name = ?, description = ?, version = ?, manifest_json = ?,
                    validated_hash = ?,
                    prepared_hash = CASE
                        WHEN prepared_hash = ? THEN prepared_hash
                        WHEN active_hash = ? THEN ?
                        ELSE NULL
                    END,
                    updated_at = ?, last_error = NULL
                WHERE id = ?
                """,
                (
                    manifest.name,
                    manifest.description,
                    manifest.version,
                    canonical_manifest_json(manifest),
                    content_hash,
                    content_hash,
                    content_hash,
                    content_hash,
                    now,
                    app.id,
                ),
            )
            if cursor.rowcount != 1:
                raise AppNotFoundError(f"App not found: {app_id_or_slug}")
        return self.require(app.id)

    def record_error(self, app_id_or_slug: str, error: str) -> AppRecord:
        app = self.require(app_id_or_slug)
        message = error.strip()[:4000] or "unknown App error"
        with get_db_connection(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE apps SET last_error = ?, updated_at = ? WHERE id = ?",
                (message, int(time.time() * 1000), app.id),
            )
        return self.require(app.id)

    def mark_prepared(
        self,
        app_id_or_slug: str,
        content_hash: str,
    ) -> AppRecord:
        app = self.require(app_id_or_slug)
        if app.validated_hash != content_hash:
            raise AppLifecycleError(
                "only the currently validated snapshot can be marked prepared"
            )
        with get_db_connection(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE apps
                SET prepared_hash = ?, updated_at = ?, last_error = NULL
                WHERE id = ? AND validated_hash = ?
                """,
                (content_hash, int(time.time() * 1000), app.id, content_hash),
            )
        return self.require(app.id)

    def activate(
        self,
        app_id_or_slug: str,
        content_hash: str | None = None,
        *,
        enable: bool = False,
    ) -> AppRecord:
        app = self.require(app_id_or_slug)
        target_hash = content_hash or app.validated_hash
        if target_hash is None or app.prepared_hash != target_hash:
            raise AppLifecycleError(
                "an App must be prepared before it can be activated"
            )
        if app.validated_hash != target_hash:
            raise AppLifecycleError(
                "only the currently validated snapshot can be activated"
            )
        with get_db_connection(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE apps
                SET active_hash = ?, enabled = ?, updated_at = ?, last_error = NULL
                WHERE id = ? AND validated_hash = ? AND prepared_hash = ?
                """,
                (
                    target_hash,
                    int(enable),
                    int(time.time() * 1000),
                    app.id,
                    target_hash,
                    target_hash,
                ),
            )
            if cursor.rowcount != 1:
                raise AppLifecycleError("App lifecycle changed while it was activating")
        return self.require(app.id)

    def set_enabled(self, app_id_or_slug: str, enabled: bool) -> AppRecord:
        app = self.require(app_id_or_slug)
        if enabled and app.active_hash is None:
            raise AppLifecycleError(
                "an App must have an active snapshot before enabling"
            )
        with get_db_connection(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE apps SET enabled = ?, updated_at = ? WHERE id = ?",
                (int(enabled), int(time.time() * 1000), app.id),
            )
        return self.require(app.id)

    def set_network_approved(
        self,
        app_id_or_slug: str,
        approved: bool,
    ) -> AppRecord:
        app = self.require(app_id_or_slug)
        with get_db_connection(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE apps
                SET network_approved = ?, updated_at = ?
                WHERE id = ?
                """,
                (int(approved), int(time.time() * 1000), app.id),
            )
        return self.require(app.id)

    def unregister(self, app_id_or_slug: str) -> AppRecord:
        app = self.require(app_id_or_slug)
        with get_db_connection(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM apps WHERE id = ?", (app.id,))
        return app

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> AppRecord:
        manifest_json = row["manifest_json"]
        manifest = None
        if manifest_json:
            try:
                manifest = parse_manifest(json.loads(manifest_json))
            except (ValueError, json.JSONDecodeError) as exc:
                raise AppRegistryError(
                    f"stored manifest for App {row['id']} is invalid"
                ) from exc
        return AppRecord(
            id=row["id"],
            slug=row["slug"],
            name=row["name"],
            description=row["description"],
            version=row["version"],
            origin=AppOrigin(row["origin"]),
            root_path=row["root_path"],
            manifest=manifest,
            validated_hash=row["validated_hash"],
            prepared_hash=row["prepared_hash"],
            active_hash=row["active_hash"],
            enabled=bool(row["enabled"]),
            network_approved=bool(row["network_approved"]),
            created_by_chat_id=row["created_by_chat_id"],
            created_by_run_id=row["created_by_run_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_error=row["last_error"],
        )
