from __future__ import annotations

import codecs
import collections
import os
import select
import signal
import subprocess
import time

from ..runtime_context import default_chat_files_cwd, resolve_chat_files_path
from ..workspace import PathAccessError
from .execution_sandbox import ExecutionSandboxUnavailable, sandboxed_command
from .toolcore import (
    failure_exit_hint,
    interpret_benign_exit_code,
    max_output_chars,
    strip_ansi,
)

RESET, DIM = "\033[0m", "\033[2m"
_READ_CHUNK = 65536


def _kill_process_group(proc: subprocess.Popen) -> None:
    """Kill the entire process group (all children).

    Ported from hermes-agent tools/environments/local.py _kill_process
    (POSIX path): SIGTERM the group, wait, escalate to SIGKILL. Waits on the
    group rather than just the shell wrapper — under load the wrapper can
    exit before grandchildren do, which would leave orphans behind.
    """

    def _group_alive(pgid: int) -> bool:
        try:
            os.killpg(pgid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # The group exists, even if this process cannot signal it.
            return True

    def _wait_for_group_exit(pgid: int, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            # Reap the wrapper promptly. A dead but unreaped group leader
            # still makes killpg(pgid, 0) report the group as alive.
            try:
                proc.poll()
            except Exception:
                pass
            if not _group_alive(pgid):
                return True
            time.sleep(0.05)
        try:
            proc.poll()
        except Exception:
            pass
        return not _group_alive(pgid)

    try:
        try:
            pgid = os.getpgid(proc.pid)
        except ProcessLookupError:
            return

        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return

        if _wait_for_group_exit(pgid, 1.0):
            return

        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            return
        _wait_for_group_exit(pgid, 2.0)
        try:
            proc.wait(timeout=0.2)
        except (subprocess.TimeoutExpired, OSError):
            pass
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except Exception:
            pass


def bash(cmd: str, timeout: float = 30, cwd: str = None):
    """Run a non-interactive shell command and return its combined output.

    On timeout the whole process group is killed (including children), so
    nothing is left running. Output is ANSI-stripped and capped; non-zero
    exit codes are reported with a short interpretation.

    Args:
        cmd: Shell command to run.
        timeout: Seconds before the command is killed (default 30).
        cwd: Working directory (default: shared applications dir).
    """
    if cwd is None:
        workdir = default_chat_files_cwd()
    else:
        try:
            workdir = resolve_chat_files_path(cwd)
        except PathAccessError as exc:
            return f"error: {exc}"
        if not workdir.is_dir():
            return f"error: cwd is not a directory: {workdir}"

    try:
        proc = subprocess.Popen(
            sandboxed_command(["/bin/bash", "--noprofile", "--norc", "-c", cmd]),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            cwd=str(workdir),
        )
    except (OSError, ExecutionSandboxUnavailable) as exc:
        return f"error: failed to start command: {exc}"

    # Bounded in-flight collection: keep the first 40% and a rolling last
    # 60% of the output cap, so a command emitting gigabytes never grows
    # memory past the cap (truncating after full capture would OOM first).
    cap = max_output_chars()
    head_cap = int(cap * 0.4)
    tail_cap = cap - head_cap
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    head: list[str] = []
    tail: collections.deque[str] = collections.deque()
    state = {"head_len": 0, "tail_len": 0, "total_len": 0}

    def _feed(text: str) -> None:
        if not text:
            return
        state["total_len"] += len(text)
        if state["head_len"] < head_cap:
            take = min(len(text), head_cap - state["head_len"])
            head.append(text[:take])
            state["head_len"] += take
            text = text[take:]
            if not text:
                return
        tail.append(text)
        state["tail_len"] += len(text)
        while tail and state["tail_len"] - len(tail[0]) >= tail_cap:
            state["tail_len"] -= len(tail.popleft())

    def _read_until(deadline: float) -> bool:
        """Read chunks until EOF or deadline; True when EOF was reached."""
        fd = proc.stdout.fileno()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                ready, _, _ = select.select([fd], [], [], min(remaining, 0.25))
            except OSError:
                return True
            if not ready:
                continue
            try:
                chunk = os.read(fd, _READ_CHUNK)
            except OSError:
                return True
            if not chunk:
                return True
            _feed(decoder.decode(chunk))

    timed_out = not _read_until(time.monotonic() + float(timeout))
    if timed_out:
        _kill_process_group(proc)
        # Writers are dead now, so EOF is guaranteed; the deadline is a
        # belt-and-braces guard while collecting what was buffered.
        _read_until(time.monotonic() + 5.0)
    _feed(decoder.decode(b"", final=True))
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    try:
        proc.stdout.close()
    except OSError:
        pass

    tail_raw = "".join(tail)
    if len(tail_raw) > tail_cap:
        tail_raw = tail_raw[-tail_cap:]
    dropped = state["total_len"] - state["head_len"] - len(tail_raw)
    if dropped > 0:
        out = (
            strip_ansi("".join(head))
            + f"\n\n... [{dropped:,} chars omitted — output truncated to {cap:,} chars] ...\n\n"
            + strip_ansi(tail_raw)
        )
    else:
        out = strip_ansi("".join(head) + tail_raw)
    out = out.strip()

    notes = []
    if timed_out:
        notes.append(
            f"timed out after {timeout:g}s — process group killed; "
            "use process_spawn for long-running commands"
        )
    elif proc.returncode not in (0, None):
        hint = interpret_benign_exit_code(cmd, proc.returncode) or failure_exit_hint(
            proc.returncode
        )
        notes.append(f"exit code {proc.returncode}" + (f" — {hint}" if hint else ""))
    for note in notes:
        out += f"\n({note})"
    out = out.strip()

    if out:
        print(f"  {DIM}│ {out}{RESET}", flush=True)
    return out or "(empty)"
