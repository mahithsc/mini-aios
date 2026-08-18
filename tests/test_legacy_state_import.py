from __future__ import annotations

import sqlite3
from pathlib import Path

from aios_core import crons, db


def _create_legacy_database(path: Path) -> None:
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE device_link (
                id INTEGER PRIMARY KEY,
                device_token TEXT NOT NULL
            );
            INSERT INTO device_link (id, device_token) VALUES (1, 'legacy-device');

            CREATE TABLE crons (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT NOT NULL,
                instructions TEXT NOT NULL,
                schedule TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_run_at TEXT,
                schedule_timezone TEXT,
                run_at_utc TEXT
            );
            INSERT INTO crons VALUES (
                'cron-1', 'legacy cron', 'description', 'instructions',
                '0 12 * * *', 'active', '2026-01-01T00:00:00+00:00', NULL,
                'America/New_York', NULL
            );

            CREATE TABLE cron_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cron_id TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                output TEXT,
                status TEXT NOT NULL
            );
            INSERT INTO cron_runs (
                cron_id, started_at, finished_at, output, status
            ) VALUES (
                'cron-1', '2026-01-01T12:00:00+00:00',
                '2026-01-01T12:00:01+00:00', 'done', 'completed'
            );
            """
        )


def test_legacy_pairing_and_crons_import_without_overwriting_canonical(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / ".mini-aios" / "state" / "aios.db"
    legacy = tmp_path / ".mini-aios" / "legacy" / "storage-layout-v1" / "state" / "aios.db"
    _create_legacy_database(legacy)

    monkeypatch.setattr(db, "DB_PATH", str(target))
    monkeypatch.setattr(db, "get_legacy_state_db_path", lambda: legacy)
    monkeypatch.setattr(crons, "DB_PATH", str(target))
    monkeypatch.setattr(crons, "get_legacy_state_db_path", lambda: legacy)

    db.initialize_app_db(str(target))
    manager = crons.CronManager(str(target))
    manager.shutdown()

    with sqlite3.connect(target) as connection:
        assert connection.execute("SELECT device_token FROM device_link").fetchone() == (
            "legacy-device",
        )
        assert connection.execute("SELECT name FROM crons WHERE id = 'cron-1'").fetchone() == (
            "legacy cron",
        )
        assert connection.execute("SELECT count(*) FROM cron_runs").fetchone() == (1,)
        connection.execute("UPDATE device_link SET device_token = 'canonical-device'")
        connection.execute("UPDATE crons SET name = 'canonical cron' WHERE id = 'cron-1'")

    db.initialize_app_db(str(target))
    second_manager = crons.CronManager(str(target))
    second_manager.shutdown()

    with sqlite3.connect(target) as connection:
        assert connection.execute("SELECT device_token FROM device_link").fetchone() == (
            "canonical-device",
        )
        assert connection.execute("SELECT name FROM crons WHERE id = 'cron-1'").fetchone() == (
            "canonical cron",
        )
        assert connection.execute("SELECT count(*) FROM cron_runs").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM legacy_cron_imports").fetchone() == (
            1,
        )
    with sqlite3.connect(legacy) as connection:
        assert connection.execute("SELECT device_token FROM device_link").fetchone() == (
            "legacy-device",
        )
