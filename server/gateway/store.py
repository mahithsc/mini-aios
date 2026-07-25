from __future__ import annotations

import json
import sqlite3
from typing import Any

from aios_core.db import DB_PATH, get_db_connection

from .schemas import utc_now_iso


def _shape_event_row(row: sqlite3.Row | tuple) -> dict[str, Any]:
    event_id, session_id, event_type, payload_json, created_at = row
    try:
        payload = json.loads(payload_json)
    except (TypeError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {"value": payload}
    return {
        "id": event_id,
        "session_id": session_id,
        "hermes_session_id": session_id,
        "type": event_type,
        "payload": payload,
        "created_at": created_at,
    }


def insert_gateway_event(
    session_id: str,
    event_type: str,
    payload: dict[str, Any],
    *,
    db_path: str = DB_PATH,
) -> dict[str, Any]:
    created_at = utc_now_iso()
    payload_json = json.dumps(payload, default=str)
    with get_db_connection(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO gateway_events (session_id, type, payload_json, created_at) "
            "VALUES (?, ?, ?, ?)",
            (session_id, event_type, payload_json, created_at),
        )
        event_id = cursor.lastrowid
    return _shape_event_row((event_id, session_id, event_type, payload_json, created_at))


def list_gateway_events_after(
    session_id: str,
    after: int = 0,
    limit: int | None = None,
    *,
    db_path: str = DB_PATH,
) -> list[dict[str, Any]]:
    query = (
        "SELECT id, session_id, type, payload_json, created_at FROM gateway_events "
        "WHERE session_id = ? AND id > ? ORDER BY id ASC"
    )
    params: list[Any] = [session_id, int(after)]
    if limit is not None:
        query += " LIMIT ?"
        params.append(int(limit))
    with get_db_connection(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
    return [_shape_event_row(row) for row in rows]
