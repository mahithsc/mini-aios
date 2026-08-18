from __future__ import annotations

import asyncio
import math
import os
import shutil
import signal
import tempfile
import threading
from pathlib import Path

from ..context import default_agent_cwd, resolve_agent_path
from .tool_output_limits import get_max_lines
from .toolcore import (
    failure_exit_hint,
    interpret_benign_exit_code,
    max_output_chars,
    strip_ansi,
)

_READ_CHUNK = 65_536
_PIPE_IDLE_GRACE_SECONDS = 0.1
_MAX_TIMEOUT_SECONDS = 2_147_483.647

_active_process_groups: set[int] = set()
_active_process_groups_lock = threading.Lock()


class _OutputAccumulator:
    """Keep a bounded output tail while securely spooling the full stream."""

    def __init__(self) -> None:
        self.max_bytes = max_output_chars()
        self.max_lines = get_max_lines()
        self.tail = bytearray()
        self.truncated = False
        fd, raw_path = tempfile.mkstemp(prefix="aios-bash-", suffix=".log")
        self.path = Path(raw_path)
        self._file = os.fdopen(fd, "wb")

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._file.write(chunk)
        self.tail.extend(chunk)

        if len(self.tail) > self.max_bytes:
            del self.tail[: len(self.tail) - self.max_bytes]
            self.truncated = True

        line_count = self.tail.count(b"\n")
        if self.tail and not self.tail.endswith(b"\n"):
            line_count += 1
        while line_count > self.max_lines:
            newline = self.tail.find(b"\n")
            if newline < 0:
                break
            del self.tail[: newline + 1]
            line_count -= 1
            self.truncated = True

    def finish(self) -> tuple[str, Path | None]:
        self._file.flush()
        self._file.close()

        raw_tail = bytes(self.tail)
        # A byte cap can land in the middle of a UTF-8 character. Drop only
        # leading continuation bytes so the retained tail starts cleanly.
        while raw_tail and raw_tail[0] & 0xC0 == 0x80:
            raw_tail = raw_tail[1:]
        text = strip_ansi(raw_tail.decode("utf-8", errors="replace")).strip()

        if self.truncated:
            return text, self.path
        self.path.unlink(missing_ok=True)
        return text, None

    def discard(self) -> None:
        try:
            self._file.close()
        finally:
            self.path.unlink(missing_ok=True)


def _resolve_shell() -> str | None:
    if Path("/bin/bash").is_file():
        return "/bin/bash"
    return shutil.which("bash") or shutil.which("sh")


def _validated_timeout(timeout: float | None) -> float | None:
    if timeout is None:
        return None
    try:
        value = float(timeout)
    except (TypeError, ValueError) as exc:
        raise ValueError("timeout must be a number of seconds") from exc
    if not math.isfinite(value) or value <= 0 or value > _MAX_TIMEOUT_SECONDS:
        raise ValueError(
            f"timeout must be finite, greater than 0, and no more than "
            f"{_MAX_TIMEOUT_SECONDS:g} seconds"
        )
    return value


def _register_process_group(pgid: int) -> None:
    with _active_process_groups_lock:
        _active_process_groups.add(pgid)


def _unregister_process_group(pgid: int) -> None:
    with _active_process_groups_lock:
        _active_process_groups.discard(pgid)


def _signal_process_group(pgid: int) -> None:
    """Immediately kill one Bash process tree, matching Pi's abort behavior."""
    try:
        if hasattr(os, "killpg"):
            os.killpg(pgid, signal.SIGKILL)
        else:
            os.kill(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


async def _kill_process_group(
    proc: asyncio.subprocess.Process,
    pgid: int,
) -> None:
    _signal_process_group(pgid)
    if proc.returncode is None:
        try:
            proc.kill()
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=2)
    except (TimeoutError, ProcessLookupError, OSError):
        pass


def close_all_shell_processes() -> None:
    """Kill Bash process groups that are still active during runtime shutdown."""
    with _active_process_groups_lock:
        process_groups = tuple(_active_process_groups)
        _active_process_groups.clear()
    for pgid in process_groups:
        _signal_process_group(pgid)


async def _settle_reader(reader_task: asyncio.Task[None]) -> None:
    if reader_task.done():
        await asyncio.gather(reader_task, return_exceptions=True)
        return
    try:
        await asyncio.wait_for(asyncio.shield(reader_task), timeout=1)
    except TimeoutError:
        reader_task.cancel()
        await asyncio.gather(reader_task, return_exceptions=True)


async def _run_bash(
    command: str,
    timeout: float | None = None,
    *,
    cwd: str | None = None,
) -> str:
    """Internal runner with a cwd override used by low-level tests."""
    try:
        timeout_value = _validated_timeout(timeout)
    except ValueError as exc:
        return f"error: {exc}"

    if cwd is None:
        workdir = default_agent_cwd()
    else:
        workdir = resolve_agent_path(cwd)
    if not workdir.is_dir():
        return f"error: working directory is not a directory: {workdir}"

    shell = _resolve_shell()
    if shell is None:
        return "error: failed to start command: bash or sh was not found"

    try:
        accumulator = _OutputAccumulator()
    except OSError as exc:
        return f"error: failed to prepare command output: {exc}"

    try:
        proc = await asyncio.create_subprocess_exec(
            shell,
            "-c",
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
            start_new_session=True,
            cwd=str(workdir),
        )
    except (OSError, ValueError) as exc:
        accumulator.discard()
        return f"error: failed to start command: {exc}"

    pgid = proc.pid
    _register_process_group(pgid)
    activity = {"version": 0}

    async def _read_output() -> None:
        assert proc.stdout is not None
        while chunk := await proc.stdout.read(_READ_CHUNK):
            accumulator.feed(chunk)
            activity["version"] += 1

    reader_task = asyncio.create_task(_read_output())

    async def _wait_for_completion() -> int:
        # asyncio's Process.wait() may wait for stdout EOF as well as process
        # exit. Poll returncode so an inherited pipe cannot prevent us from
        # noticing that the shell itself is already gone.
        while proc.returncode is None:
            if reader_task.done() and not reader_task.cancelled():
                if reader_error := reader_task.exception():
                    raise reader_error
            await asyncio.sleep(0.01)
        returncode = proc.returncode

        if reader_task.done() and not reader_task.cancelled():
            if reader_error := reader_task.exception():
                raise reader_error

        # A background descendant can inherit stdout after the shell exits.
        # Accept late output, resetting a short grace period on each chunk,
        # then kill a quiet leftover tree instead of hanging until timeout.
        version = activity["version"]
        loop = asyncio.get_running_loop()
        idle_deadline = loop.time() + _PIPE_IDLE_GRACE_SECONDS
        while not reader_task.done():
            remaining = idle_deadline - loop.time()
            if remaining <= 0:
                await _kill_process_group(proc, pgid)
                break
            await asyncio.sleep(min(0.01, remaining))
            if activity["version"] != version:
                version = activity["version"]
                idle_deadline = loop.time() + _PIPE_IDLE_GRACE_SECONDS
        await _settle_reader(reader_task)
        if not reader_task.cancelled():
            if reader_error := reader_task.exception():
                raise reader_error
        return returncode

    timed_out = False
    try:
        if timeout_value is None:
            returncode = await _wait_for_completion()
        else:
            returncode = await asyncio.wait_for(
                _wait_for_completion(), timeout=timeout_value
            )
    except TimeoutError:
        timed_out = True
        await _kill_process_group(proc, pgid)
        await _settle_reader(reader_task)
        returncode = proc.returncode
    except asyncio.CancelledError:
        # Agno awaits async tools directly. Re-raising cancels the run, while
        # killing first ensures its OS process tree cannot outlive the run.
        await _kill_process_group(proc, pgid)
        await _settle_reader(reader_task)
        accumulator.discard()
        raise
    except BaseException:
        await _kill_process_group(proc, pgid)
        await _settle_reader(reader_task)
        accumulator.discard()
        raise
    finally:
        _unregister_process_group(pgid)

    out, full_output_path = accumulator.finish()
    notes: list[str] = []
    if full_output_path is not None:
        notes.append(
            f"output truncated to the last {accumulator.max_lines:,} lines or "
            f"{accumulator.max_bytes:,} bytes; full output: {full_output_path}"
        )
    if timed_out:
        notes.append(f"timed out after {timeout_value:g}s — process group killed")
    elif returncode not in (0, None):
        hint = interpret_benign_exit_code(command, returncode) or failure_exit_hint(
            returncode
        )
        notes.append(f"exit code {returncode}" + (f" — {hint}" if hint else ""))

    if notes:
        note_text = "\n".join(f"({note})" for note in notes)
        out = f"{out}\n{note_text}" if out else note_text
    return out or "(no output)"


async def bash(command: str, timeout: float | None = None) -> str:
    """Run a command in a fresh non-interactive Bash process.

    Stdout and stderr are combined in arrival order. Output is limited to a
    bounded tail; when truncated, the complete output is saved to a temporary
    log. Timeout or agent cancellation kills the entire process group.

    Args:
        command: Shell command to run in the current chat scratch directory.
        timeout: Optional timeout in seconds. By default there is no timeout.
    """
    return await _run_bash(command, timeout)
