import logging
import os
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone

from .agent import create_agent
from .prompt_loader import load_prompt
from .workspace import ensure_workspace_dir


log = logging.getLogger(__name__)
_WORKSPACE_DIR = ensure_workspace_dir()
HEARTBEAT_LOG_DIR = str(_WORKSPACE_DIR / "heartbeat_logs")
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 120


class HeartbeatService:
    def __init__(self, interval_seconds: int = DEFAULT_HEARTBEAT_INTERVAL_SECONDS):
        self.interval_seconds = max(1, int(interval_seconds))
        self._executor: ThreadPoolExecutor | None = None
        self._stop_event = threading.Event()
        self._pulse_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._active_future: Future | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        os.makedirs(HEARTBEAT_LOG_DIR, exist_ok=True)
        self._executor = ThreadPoolExecutor(max_workers=1)
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name="heartbeat-service", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1)
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=False)
            self._executor = None
        self._thread = None
        self._active_future = None

    def _run_loop(self) -> None:
        # Kick off one pulse at startup, then continue on the configured interval.
        while not self._stop_event.is_set():
            self._submit_pulse()
            if self._stop_event.wait(self.interval_seconds):
                break

    def _submit_pulse(self) -> None:
        with self._pulse_lock:
            if self._active_future and not self._active_future.done():
                log.info("Skipping heartbeat pulse because the previous pulse is still running")
                return
            if self._executor is None:
                return
            self._active_future = self._executor.submit(self._run_pulse)

    def _run_pulse(self) -> None:
        started = datetime.now(timezone.utc).isoformat()
        status = "completed"
        output = ""

        try:
            agent = create_agent()
            response = agent.run(load_prompt("heartbeat.md"))
            output = (response.content or "").strip()
        except Exception as exc:
            status = "error"
            output = str(exc)
            log.error("Heartbeat pulse failed: %s", exc)

        finished = datetime.now(timezone.utc).isoformat()
        log_path = os.path.join(HEARTBEAT_LOG_DIR, f"{started.replace(':', '-')}.log")
        with open(log_path, "w") as file:
            file.write(
                f"started: {started}\nfinished: {finished}\nstatus: {status}\n\n"
            )
            file.write(output)


heartbeat_service = HeartbeatService(
    interval_seconds=int(os.getenv("AIOS_HEARTBEAT_INTERVAL_SECONDS", DEFAULT_HEARTBEAT_INTERVAL_SECONDS))
)
