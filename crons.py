import sqlite3
import uuid
import logging
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

DB_PATH = "crons.db"
CRON_LOG_DIR = "cron_logs"
log = logging.getLogger(__name__)


class CronManager:
    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        self._init_db()
        self._pool = ThreadPoolExecutor(max_workers=4)
        self._scheduler = None

    def _get_conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_db(self):
        with self._get_conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS crons (
                    id              TEXT PRIMARY KEY,
                    name            TEXT NOT NULL,
                    description     TEXT NOT NULL,
                    instructions    TEXT NOT NULL,
                    schedule        TEXT NOT NULL,
                    status          TEXT NOT NULL DEFAULT 'active',
                    created_at      TEXT NOT NULL,
                    last_run_at     TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_crons_status
                    ON crons(status);

                CREATE TABLE IF NOT EXISTS cron_runs (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    cron_id     TEXT NOT NULL REFERENCES crons(id),
                    started_at  TEXT NOT NULL,
                    finished_at TEXT,
                    output      TEXT,
                    status      TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_cron_runs_cron_id
                    ON cron_runs(cron_id);
            """)

    @staticmethod
    def _validate_schedule(schedule: str):
        """Raise ValueError if schedule is not a valid cron expression."""
        CronTrigger.from_crontab(schedule)

    def start(self):
        """Load active crons from DB and start the background scheduler."""
        import os
        os.makedirs(CRON_LOG_DIR, exist_ok=True)

        self._scheduler = BackgroundScheduler(daemon=True)

        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT id, name, instructions, schedule FROM crons WHERE status = 'active'"
            ).fetchall()

        for cron_id, name, instructions, schedule in rows:
            trigger = CronTrigger.from_crontab(schedule)
            self._scheduler.add_job(
                self._run_cron,
                trigger=trigger,
                args=[cron_id, instructions],
                id=cron_id,
                name=name,
                replace_existing=True,
            )
            log.info("Loaded cron %s (%s) schedule=%s", cron_id[:8], name, schedule)

        self._scheduler.start()
        log.info("Cron scheduler started with %d jobs", len(rows))

    def create_cron(self, name: str, description: str, instructions: str, schedule: str) -> str:
        """Insert a new cron into the DB and register it with the scheduler."""
        self._validate_schedule(schedule)
        cron_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute(
                "INSERT INTO crons (id, name, description, instructions, schedule, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, 'active', ?)",
                (cron_id, name, description, instructions, schedule, now),
            )

        if self._scheduler and self._scheduler.running:
            trigger = CronTrigger.from_crontab(schedule)
            self._scheduler.add_job(
                self._run_cron,
                trigger=trigger,
                args=[cron_id, instructions],
                id=cron_id,
                name=name,
                replace_existing=True,
            )

        return cron_id

    def delete_cron(self, cron_id: str) -> str:
        """Soft-delete: set status to paused and remove from scheduler."""
        with self._get_conn() as conn:
            cursor = conn.execute(
                "UPDATE crons SET status = 'paused' WHERE id = ? AND status = 'active'",
                (cron_id,),
            )
            if cursor.rowcount == 0:
                return f"error: no active cron with id {cron_id}"

        if self._scheduler and self._scheduler.running:
            try:
                self._scheduler.remove_job(cron_id)
            except Exception:
                pass

        return "ok"

    def edit_cron(self, cron_id: str, **fields) -> str:
        """Update fields on an existing cron."""
        allowed = {"name", "description", "instructions", "schedule"}
        updates = [(k, v) for k, v in fields.items() if k in allowed and v is not None]
        if not updates:
            return "nothing to update"

        for key, val in updates:
            if key == "schedule":
                self._validate_schedule(val)

        set_clause = ", ".join(f"{k} = ?" for k, _ in updates)
        values = [v for _, v in updates]

        with self._get_conn() as conn:
            cursor = conn.execute(
                f"UPDATE crons SET {set_clause} WHERE id = ? AND status = 'active'",
                (*values, cron_id),
            )
            if cursor.rowcount == 0:
                return f"error: no active cron with id {cron_id}"
        return "ok"

    def list_crons(self) -> str:
        """Return a formatted string of all active crons."""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT id, name, description, schedule, created_at, last_run_at "
                "FROM crons WHERE status = 'active' ORDER BY created_at"
            ).fetchall()
        if not rows:
            return "No active crons."
        lines = []
        for cid, name, description, schedule, created, last_run in rows:
            lines.append(
                f"- [{cid[:8]}] {name}: {description}\n"
                f"  schedule: {schedule}  |  last run: {last_run or 'never'}"
            )
        return "\n".join(lines)

    def _run_cron(self, cron_id: str, instructions: str):
        """Spawn an agent and run the cron instructions. Called by the scheduler."""
        import os

        started = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute(
                "INSERT INTO cron_runs (cron_id, started_at, status) VALUES (?, ?, 'running')",
                (cron_id, started),
            )
            run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        output = ""
        status = "completed"
        try:
            from agent import create_agent
            agent = create_agent()
            messages = [{"role": "user", "content": instructions}]
            response = agent.run(messages)
            output = response.content or ""
        except Exception as e:
            status = "error"
            output = str(e)
            log.error("Cron %s failed: %s", cron_id[:8], e)

        finished = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE cron_runs SET finished_at = ?, output = ?, status = ? WHERE id = ?",
                (finished, output[:5000], status, run_id),
            )
            conn.execute(
                "UPDATE crons SET last_run_at = ? WHERE id = ?",
                (finished, cron_id),
            )

        log_path = os.path.join(CRON_LOG_DIR, f"{cron_id[:8]}_{finished.replace(':', '-')}.log")
        with open(log_path, "w") as f:
            f.write(f"cron_id: {cron_id}\nstarted: {started}\nfinished: {finished}\nstatus: {status}\n\n")
            f.write(output)

        log.info("Cron %s finished (%s)", cron_id[:8], status)

    def shutdown(self):
        """Stop scheduler and worker pool."""
        if self._scheduler and self._scheduler.running:
            self._scheduler.shutdown(wait=False)
        self._pool.shutdown(wait=True, cancel_futures=True)


cron_manager = CronManager()
