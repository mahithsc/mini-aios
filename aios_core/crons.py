import sqlite3
import uuid
import logging
import os
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.cron import CronTrigger
from .agent import create_agent
from .prompt_loader import render_prompt
from .workspace import ensure_workspace_dir

_WORKSPACE_DIR = ensure_workspace_dir()
DB_PATH = str(_WORKSPACE_DIR / "crons.db")
CRON_LOG_DIR = str(_WORKSPACE_DIR / "cron_logs")
DEFAULT_CRON_TIMEZONE = os.getenv("AIOS_DEFAULT_TIMEZONE", "America/New_York")
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

            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(crons)")
            }
            if "schedule_timezone" not in columns:
                conn.execute("ALTER TABLE crons ADD COLUMN schedule_timezone TEXT")
            if "run_at_utc" not in columns:
                conn.execute("ALTER TABLE crons ADD COLUMN run_at_utc TEXT")

            conn.execute(
                "UPDATE crons SET schedule_timezone = ? "
                "WHERE (schedule_timezone IS NULL OR schedule_timezone = '') AND schedule != ''",
                (DEFAULT_CRON_TIMEZONE,),
            )

    @staticmethod
    def _get_timezone(timezone_name: str | None) -> ZoneInfo:
        timezone_name = timezone_name or DEFAULT_CRON_TIMEZONE
        try:
            return ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone '{timezone_name}'") from exc

    @classmethod
    def _normalize_timezone_name(cls, timezone_name: str | None) -> str:
        return cls._get_timezone(timezone_name).key

    @classmethod
    def _validate_schedule(cls, schedule: str, timezone_name: str | None):
        """Raise ValueError if schedule is not a valid cron expression."""
        CronTrigger.from_crontab(schedule, timezone=cls._get_timezone(timezone_name))

    @staticmethod
    def _parse_run_at_utc(run_at_utc: str) -> datetime:
        try:
            run_at = datetime.fromisoformat(run_at_utc)
        except ValueError as exc:
            raise ValueError("run_at_utc must be an ISO-8601 timestamp") from exc

        if run_at.tzinfo is None:
            raise ValueError("run_at_utc must include timezone information")

        return run_at.astimezone(timezone.utc)

    @classmethod
    def _build_trigger(
        cls,
        schedule: str,
        schedule_timezone: str | None,
        run_at_utc: str | None,
    ) -> tuple[object, bool]:
        if run_at_utc:
            return DateTrigger(run_date=cls._parse_run_at_utc(run_at_utc)), True

        if not schedule:
            raise ValueError("cron jobs require either schedule or run_at_utc")

        timezone_name = cls._normalize_timezone_name(schedule_timezone)
        cls._validate_schedule(schedule, timezone_name)
        return (
            CronTrigger.from_crontab(schedule, timezone=cls._get_timezone(timezone_name)),
            False,
        )

    def _register_job(
        self,
        cron_id: str,
        name: str,
        instructions: str,
        schedule: str,
        schedule_timezone: str | None,
        run_at_utc: str | None,
    ) -> None:
        if self._scheduler is None:
            return

        trigger, one_time = self._build_trigger(schedule, schedule_timezone, run_at_utc)
        self._scheduler.add_job(
            self._run_cron,
            trigger=trigger,
            args=[cron_id, instructions, one_time],
            id=cron_id,
            name=name,
            replace_existing=True,
            misfire_grace_time=None,
        )

    def start(self):
        """Load active crons from DB and start the background scheduler."""
        os.makedirs(CRON_LOG_DIR, exist_ok=True)

        self._scheduler = BackgroundScheduler(daemon=True)

        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT id, name, instructions, schedule, schedule_timezone, run_at_utc "
                "FROM crons WHERE status = 'active'"
            ).fetchall()

        for cron_id, name, instructions, schedule, schedule_timezone, run_at_utc in rows:
            try:
                self._register_job(
                    cron_id,
                    name,
                    instructions,
                    schedule,
                    schedule_timezone,
                    run_at_utc,
                )
                if run_at_utc:
                    log.info("Loaded one-time cron %s (%s) run_at_utc=%s", cron_id[:8], name, run_at_utc)
                else:
                    log.info(
                        "Loaded cron %s (%s) schedule=%s timezone=%s",
                        cron_id[:8],
                        name,
                        schedule,
                        schedule_timezone or DEFAULT_CRON_TIMEZONE,
                    )
            except ValueError as exc:
                log.error("Skipping invalid cron %s (%s): %s", cron_id[:8], name, exc)

        self._scheduler.start()
        log.info("Cron scheduler started with %d jobs", len(rows))

    def create_cron(
        self,
        name: str,
        description: str,
        instructions: str,
        schedule: str | None = None,
        schedule_timezone: str | None = None,
        run_at_utc: str | None = None,
    ) -> str:
        """Insert a new cron into the DB and register it with the scheduler."""
        schedule = (schedule or "").strip()
        run_at_utc = (run_at_utc or "").strip()
        if bool(schedule) == bool(run_at_utc):
            raise ValueError("create requires exactly one of schedule or run_at_utc")

        timezone_name = (
            self._normalize_timezone_name(schedule_timezone) if schedule else None
        )
        if schedule:
            self._validate_schedule(schedule, timezone_name)
        else:
            run_at_utc = self._parse_run_at_utc(run_at_utc).isoformat()

        cron_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute(
                "INSERT INTO crons "
                "(id, name, description, instructions, schedule, schedule_timezone, run_at_utc, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)",
                (cron_id, name, description, instructions, schedule, timezone_name, run_at_utc, now),
            )

        if self._scheduler and self._scheduler.running:
            self._register_job(
                cron_id,
                name,
                instructions,
                schedule,
                timezone_name,
                run_at_utc,
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
        allowed = {"name", "description", "instructions", "schedule", "schedule_timezone", "run_at_utc"}
        updates = [(k, v) for k, v in fields.items() if k in allowed and v is not None]
        if not updates:
            return "nothing to update"

        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT name, description, instructions, schedule, schedule_timezone, run_at_utc "
                "FROM crons WHERE id = ? AND status = 'active'",
                (cron_id,),
            ).fetchone()
            if row is None:
                return f"error: no active cron with id {cron_id}"

            current = {
                "name": row[0],
                "description": row[1],
                "instructions": row[2],
                "schedule": row[3],
                "schedule_timezone": row[4],
                "run_at_utc": row[5],
            }

            merged = {**current}
            for key, value in updates:
                merged[key] = value

            merged["schedule"] = (merged["schedule"] or "").strip()
            merged["run_at_utc"] = (merged["run_at_utc"] or "").strip()

            if bool(merged["schedule"]) == bool(merged["run_at_utc"]):
                return "error: cron must have exactly one of schedule or run_at_utc"

            if merged["schedule"]:
                merged["schedule_timezone"] = self._normalize_timezone_name(merged["schedule_timezone"])
                self._validate_schedule(merged["schedule"], merged["schedule_timezone"])
                merged["run_at_utc"] = None
            else:
                merged["run_at_utc"] = self._parse_run_at_utc(merged["run_at_utc"]).isoformat()
                merged["schedule_timezone"] = None
                merged["schedule"] = ""

            set_clause = ", ".join(f"{k} = ?" for k in merged.keys())
            values = list(merged.values())

            conn.execute(
                f"UPDATE crons SET {set_clause} WHERE id = ? AND status = 'active'",
                (*values, cron_id),
            )

        if self._scheduler and self._scheduler.running:
            self._register_job(
                cron_id,
                merged["name"],
                merged["instructions"],
                merged["schedule"],
                merged["schedule_timezone"],
                merged["run_at_utc"],
            )
        return "ok"

    def list_crons(self) -> str:
        """Return a formatted string of all active crons."""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT id, name, description, schedule, schedule_timezone, run_at_utc, created_at, last_run_at "
                "FROM crons WHERE status = 'active' ORDER BY created_at"
            ).fetchall()
        if not rows:
            return "No active crons."
        lines = []
        for cid, name, description, schedule, schedule_timezone, run_at_utc, created, last_run in rows:
            timing = (
                f"run_at_utc: {run_at_utc}"
                if run_at_utc
                else f"schedule: {schedule} ({schedule_timezone or DEFAULT_CRON_TIMEZONE})"
            )
            lines.append(f"- [{cid[:8]}] {name}: {description}\n  {timing}  |  last run: {last_run or 'never'}")
        return "\n".join(lines)

    def _run_cron(self, cron_id: str, instructions: str, one_time: bool = False):
        """Spawn an agent and run the cron instructions. Called by the scheduler."""
        started = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute(
                "INSERT INTO cron_runs (cron_id, started_at, status) VALUES (?, ?, 'running')",
                (cron_id, started),
            )
            run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        output = ""
        status = "completed"
        
        prompt = render_prompt("cron.md", instructions=instructions)
        try:
            agent = create_agent()
            messages = [{"role": "user", "content": prompt}]
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
            if one_time:
                conn.execute(
                    "UPDATE crons SET last_run_at = ?, status = 'paused' WHERE id = ?",
                    (finished, cron_id),
                )
            else:
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
