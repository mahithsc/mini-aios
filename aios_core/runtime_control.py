from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .workspace import ensure_workspace_dir


class RuntimeDrainingError(RuntimeError):
    """Raised when new work is submitted while an update drain is active."""


@dataclass(frozen=True)
class DrainSnapshot:
    draining: bool
    requested_at: int | None
    reason: str | None
    active_runs: int

    def as_dict(self) -> dict[str, object]:
        return {
            "draining": self.draining,
            "requestedAt": self.requested_at,
            "reason": self.reason,
            "activeRuns": self.active_runs,
        }


class RuntimeControl:
    """Durable update-drain flag shared across application restarts."""

    def __init__(self, state_path: Path) -> None:
        self._state_path = state_path
        self._lock = threading.RLock()

    def _read(self) -> tuple[bool, int | None, str | None]:
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return False, None, None
        if not isinstance(payload, dict):
            return False, None, None
        draining = payload.get("draining") is True
        requested_at = payload.get("requestedAt")
        reason = payload.get("reason")
        return (
            draining,
            requested_at if isinstance(requested_at, int) else None,
            reason if isinstance(reason, str) else None,
        )

    def _write(self, payload: dict[str, object]) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".runtime-control.",
            dir=str(self._state_path.parent),
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, self._state_path)
            directory_descriptor = os.open(self._state_path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        finally:
            Path(temporary_name).unlink(missing_ok=True)

    def request_drain(self, reason: str = "update") -> DrainSnapshot:
        with self._lock:
            draining, requested_at, previous_reason = self._read()
            if not draining:
                requested_at = int(time.time() * 1000)
                previous_reason = reason[:160]
                self._write(
                    {
                        "draining": True,
                        "requestedAt": requested_at,
                        "reason": previous_reason,
                    }
                )
            return DrainSnapshot(True, requested_at, previous_reason, 0)

    def resume(self) -> DrainSnapshot:
        with self._lock:
            self._write(
                {
                    "draining": False,
                    "requestedAt": None,
                    "reason": None,
                }
            )
            return DrainSnapshot(False, None, None, 0)

    def snapshot(self, *, active_runs: int = 0) -> DrainSnapshot:
        with self._lock:
            draining, requested_at, reason = self._read()
            return DrainSnapshot(
                draining=draining,
                requested_at=requested_at,
                reason=reason,
                active_runs=max(0, active_runs),
            )

    def ensure_accepting_work(self) -> None:
        if self.snapshot().draining:
            raise RuntimeDrainingError(
                "Mini AIOS is draining for an update; retry shortly."
            )


_runtime_control: RuntimeControl | None = None
_runtime_control_lock = threading.Lock()


def get_runtime_control() -> RuntimeControl:
    global _runtime_control
    if _runtime_control is None:
        with _runtime_control_lock:
            if _runtime_control is None:
                _runtime_control = RuntimeControl(
                    ensure_workspace_dir() / "update-drain.json"
                )
    return _runtime_control
