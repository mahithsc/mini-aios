"""Tier 3 behavior evals for the codex_subagent tool.

These run the REAL main agent against fixed prompts and score three things the
user cares about:

  1. Called appropriately   - the agent delegates to codex_subagent when it should
  2. Not over-delegating    - the agent does NOT delegate for trivial work
  3. Correct instructions   - the `task` it passes Codex is self-contained/actionable
  4. Delegation succeeds     - end-to-end, Codex produces the artifact (real CLI)

Non-deterministic, so each case runs K times (default 5) and must clear a
pass-rate / judge-score threshold (default 0.8). Writes a JSON scorecard the
overnight iteration loop reads to decide "done".

Run:  PYTHONPATH=. uv run python evals/codex_subagent_eval.py
Cost: makes real gpt-4.1 calls (agent + judge); the e2e case also runs Codex.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import Callable

from agno.agent import Agent
from agno.models.openai import OpenAIChat
from agno.run.agent import RunEvent as AgentRunEvent
from contextlib import contextmanager

from dotenv import load_dotenv

# Ensure OPENAI_API_KEY (and friends) are present regardless of which case runs
# first. The simulator/judge build ``OpenAI()`` directly, so we can't rely on
# the ``load_dotenv()`` side effect that fires when ``aios_core.agent`` is first
# imported (a case-ordering trap when a later tier is run in isolation).
load_dotenv()


@contextmanager
def _chat_files_dir(workdir: "Path"):
    """Point relative file-tool and Codex paths at ``workdir`` for the duration,
    mirroring the box runtime (``push_chat_runtime_context``). Without this the
    agent inspects the SHARED workspace, finds stale artifacts from earlier runs,
    hallucinates the task is already done, and never delegates — while ``workdir``
    stays empty and verification fails. Single-turn e2e cases dodge this by
    embedding the absolute path in the prompt; the multi-turn simulator paraphrases
    and drops it, so isolation must be enforced here."""
    from aios_core import runtime_context as rc

    token = rc._CURRENT_CHAT_FILES_DIR.set(str(workdir))
    try:
        yield
    finally:
        rc._CURRENT_CHAT_FILES_DIR.reset(token)

DEFAULT_K = int(os.getenv("EVAL_K", "5"))
DEFAULT_THRESHOLD = float(os.getenv("EVAL_THRESHOLD", "0.8"))
MODEL_ID = os.getenv("AIOS_MODEL_ID", "gpt-4.1")

# E2E cases are grouped into difficulty tiers. The overnight loop starts at a
# low tier and raises EVAL_MAX_TIER as lower tiers stabilize ("as the night
# progresses"). Only cases with tier <= max_tier are attempted.
DEFAULT_MAX_TIER = int(os.getenv("EVAL_MAX_TIER", "2"))

# Global caps so an unattended overnight loop can't run away with time/cost.
# Counted across the whole eval invocation; e2e cases stop launching once hit.
MAX_E2E_RUNS = int(os.getenv("EVAL_MAX_E2E_RUNS", "24"))
MAX_MINUTES = float(os.getenv("EVAL_MAX_MINUTES", "180"))


class _Budget:
    """Tracks real-Codex e2e runs + wall-clock against the global caps."""

    def __init__(self) -> None:
        self.e2e_runs = 0
        self.start = monotonic()

    def available(self) -> tuple[bool, str]:
        if self.e2e_runs >= MAX_E2E_RUNS:
            return False, f"e2e run cap ({MAX_E2E_RUNS}) reached"
        elapsed_min = (monotonic() - self.start) / 60
        if elapsed_min >= MAX_MINUTES:
            return False, f"time cap ({MAX_MINUTES:g}m) reached"
        return True, ""

    def record(self) -> None:
        self.e2e_runs += 1


BUDGET = _Budget()


# --------------------------------------------------------------------------- #
# Agent construction with a recording stub for the preferred codex_start path
# --------------------------------------------------------------------------- #


def _make_recording_codex(sink: list[dict]) -> Callable:
    """A stand-in for codex_start that records the args the agent passes and
    returns a canned success — so decision/instruction cases don't pay for a
    real Codex run. Mirrors the real tool's name + docstring + signature so the
    model's view of the tool is identical."""
    from aios_core.tools.codex_job import codex_start as real

    def codex_start(task=None, path=".", model=None, deploy=False, fc=None):
        sink.append(
            {"task": task, "model": model, "path": path, "deploy": deploy}
        )
        return {
            "job_id": "recording-job",
            "status": "running",
            "workdir": path,
            "auto_continuation": True,
        }

    codex_start.__doc__ = real.__doc__
    return codex_start


def _build_agent(record_sink: list[dict] | None) -> Agent:
    from aios_core.agent import MAIN_TOOLS, _build_prompt, DEFAULT_MODEL_ID

    tools = []
    for tool in MAIN_TOOLS:
        if getattr(tool, "__name__", "") == "codex_start" and record_sink is not None:
            tools.append(_make_recording_codex(record_sink))
        else:
            tools.append(tool)
    return Agent(
        system_message=_build_prompt(include_subagent_tool=True),
        tools=tools,
        model=OpenAIChat(id=DEFAULT_MODEL_ID),
    )


def _run_messages(agent: Agent, messages: list[dict]) -> tuple[list[dict], str]:
    """Run one agent turn over a full message history; return (codex_calls, text).

    Mirrors the box's runtime (server/execution/runners/chat.py): pass the whole
    [{role, content}] history each turn so the agent has the conversation."""
    calls: list[dict] = []
    final_chunks: list[str] = []
    for event in agent.run(messages, stream=True, stream_events=True):
        if event.event == AgentRunEvent.tool_call_started and event.tool is not None:
            if event.tool.tool_name in {"codex_start", "codex_subagent"}:
                calls.append({"args": event.tool.tool_args, "result": None})
        elif event.event == AgentRunEvent.tool_call_completed and event.tool is not None:
            if event.tool.tool_name in {"codex_start", "codex_subagent"} and calls:
                calls[-1]["result"] = str(event.tool.result)[:400]
        elif event.event == AgentRunEvent.run_content and event.content is not None:
            final_chunks.append(str(event.content))
    return calls, "".join(final_chunks)


def _run_agent(prompt: str, record_sink: list[dict] | None) -> tuple[list[dict], str]:
    """Single-turn convenience: run the agent once on one user prompt."""
    agent = _build_agent(record_sink)
    return _run_messages(agent, [{"role": "user", "content": prompt}])


# --------------------------------------------------------------------------- #
# LLM judge for instruction quality
# --------------------------------------------------------------------------- #

_JUDGE_RUBRIC = """You are grading the QUALITY of an instruction that a main agent
passed to a Codex coding subagent. The original user request and the delegated
task are below.

Score 0.0-1.0 on whether the delegated task is a GOOD instruction:
- self-contained (Codex can act without seeing the chat)
- restates the concrete goal from the user request
- includes the target file/path and any needed context
- actionable and unambiguous

Return ONLY minified JSON: {"score": <float 0-1>, "reason": "<short>"}

USER REQUEST:
<<USER_REQUEST>>

DELEGATED TASK:
<<DELEGATED_TASK>>
"""


def _judge_instruction(user_request: str, delegated_task: str) -> float:
    from openai import OpenAI

    client = OpenAI()
    prompt = (
        _JUDGE_RUBRIC.replace("<<USER_REQUEST>>", user_request)
        .replace("<<DELEGATED_TASK>>", delegated_task or "(none)")
    )
    resp = client.chat.completions.create(
        model=MODEL_ID,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
    )
    raw = (resp.choices[0].message.content or "").strip()
    # Tolerate ```json fences or prose around the JSON object.
    if "{" in raw and "}" in raw:
        raw = raw[raw.index("{") : raw.rindex("}") + 1]
    try:
        return float(json.loads(raw).get("score", 0.0))
    except Exception:
        return 0.0


# --------------------------------------------------------------------------- #
# Cases
# --------------------------------------------------------------------------- #


@dataclass
class CaseResult:
    name: str
    dimension: str
    runs: int
    passes: int
    score: float
    threshold: float
    passed: bool
    notes: list[str] = field(default_factory=list)
    tier: int = 1
    skipped: bool = False  # gated by tier or budget; excluded from all_passed


POSITIVE_PROMPT = (
    "Use your Codex coding agent to implement a function add(a, b) that returns their "
    "sum, written to a file calc.py in the current directory."
)
NEGATIVE_PROMPTS = [
    "What is 2 + 2? Answer directly in one word.",
    "Say hello to me.",
]


def eval_called_appropriately(k: int, threshold: float) -> CaseResult:
    passes, notes = 0, []
    for i in range(k):
        sink: list[dict] = []
        calls, _ = _run_agent(POSITIVE_PROMPT, sink)
        if calls:
            passes += 1
        else:
            notes.append(f"run {i}: did NOT delegate")
    score = passes / k
    return CaseResult("called_appropriately", "called-appropriately", k, passes, score, threshold, score >= threshold, notes)


def eval_no_over_delegation(k: int, threshold: float) -> CaseResult:
    passes, notes = 0, []
    for i in range(k):
        prompt = NEGATIVE_PROMPTS[i % len(NEGATIVE_PROMPTS)]
        sink: list[dict] = []
        calls, _ = _run_agent(prompt, sink)
        if not calls:
            passes += 1
        else:
            notes.append(f"run {i}: over-delegated on {prompt!r}")
    score = passes / k
    return CaseResult("no_over_delegation", "not-over-delegating", k, passes, score, threshold, score >= threshold, notes)


def eval_instruction_quality(k: int, threshold: float) -> CaseResult:
    """Judge the QUALITY of the task the agent hands Codex — but only over runs
    where it actually delegated. Delegation *rate* is measured separately by
    called_appropriately; scoring non-delegation as 0 here would double-penalize
    it and muddy the signal. If nothing delegated across k runs, mark skipped."""
    scores, notes = [], []
    for i in range(k):
        sink: list[dict] = []
        _run_agent(POSITIVE_PROMPT, sink)
        if not sink:
            notes.append(f"run {i}: no delegation (not judged; see called_appropriately)")
            continue
        task = sink[-1].get("task") or ""
        s = _judge_instruction(POSITIVE_PROMPT, task)
        scores.append(s)
        if s < threshold:
            notes.append(f"run {i}: weak task (score {s:.2f}): {task[:80]!r}")
    if not scores:
        notes.append("no delegated runs to judge across k attempts")
        return CaseResult("instruction_quality", "correct-instructions", k, 0, 0.0,
                          threshold, False, notes, skipped=True)
    avg = sum(scores) / len(scores)
    return CaseResult("instruction_quality", "correct-instructions", len(scores),
                      sum(1 for s in scores if s >= threshold), avg, threshold,
                      avg >= threshold, notes)


# --------------------------------------------------------------------------- #
# End-to-end cases (real Codex, real artifacts). Multi-step tasks that force
# Codex through several internal turns: create multiple files, wire them
# together, and (for the server case) produce something that actually runs.
# --------------------------------------------------------------------------- #


def _free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _verify_add_function(workdir: Path, port: int) -> tuple[bool, str]:
    calc = workdir / "calc.py"
    if not calc.exists():
        return False, "calc.py missing"
    if "def add" not in calc.read_text():
        return False, "no add() in calc.py"
    return True, "ok"


def _verify_static_site(workdir: Path, port: int) -> tuple[bool, str]:
    index = workdir / "index.html"
    css = workdir / "styles.css"
    if not index.exists():
        return False, "index.html missing"
    html = index.read_text().lower()
    if "<h1" not in html:
        return False, "no <h1> in index.html"
    if "styles.css" not in html:
        return False, "index.html does not link styles.css"
    if not css.exists():
        return False, "styles.css missing"
    if "background" not in css.read_text().lower():
        return False, "styles.css has no background rule"
    return True, "ok"


def _verify_running_server(workdir: Path, port: int) -> tuple[bool, str]:
    """Independently start the server Codex built and confirm it serves the
    expected page. This is the real signal that Codex produced a working,
    startable website — not just files on disk."""
    import subprocess
    import time
    import urllib.request

    server = workdir / "server.py"
    if not server.exists():
        return False, "server.py missing"

    proc = subprocess.Popen(
        ["python", "server.py"],
        cwd=str(workdir),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        body = ""
        for _ in range(30):  # up to ~6s for the server to come up
            if proc.poll() is not None:
                return False, f"server.py exited early (rc={proc.returncode})"
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1) as resp:
                    body = resp.read().decode(errors="replace")
                    break
            except Exception:
                time.sleep(0.2)
        else:
            return False, f"server did not respond on port {port}"
        if "hello from codex" not in body.lower():
            return False, "response missing 'Hello from Codex'"
        return True, "ok"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


def _run_workdir_script(workdir: Path, script: str, timeout: int = 30) -> tuple[int, str]:
    import subprocess

    proc = subprocess.run(
        ["python", script],
        cwd=str(workdir),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return proc.returncode, (proc.stdout + proc.stderr)


def _verify_tests_pass(workdir: Path, port: int) -> tuple[bool, str]:
    if not (workdir / "tests.py").exists():
        return False, "tests.py missing"
    try:
        rc, out = _run_workdir_script(workdir, "tests.py")
    except Exception as exc:
        return False, f"tests.py did not run: {exc}"
    return (rc == 0, "ok" if rc == 0 else f"tests.py failed (rc={rc}): {out[-200:]}")


def _verify_json_api(workdir: Path, port: int) -> tuple[bool, str]:
    """Start the API Codex built, POST an item, GET the list, confirm it persisted."""
    import json as _json
    import subprocess
    import time
    import urllib.request

    if not (workdir / "server.py").exists():
        return False, "server.py missing"
    proc = subprocess.Popen(
        ["python", "server.py"], cwd=str(workdir),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(30):
            if proc.poll() is not None:
                return False, f"server exited early (rc={proc.returncode})"
            try:
                urllib.request.urlopen(f"{base}/api/items", timeout=1)
                break
            except Exception:
                time.sleep(0.2)
        else:
            return False, f"server did not respond on port {port}"
        req = urllib.request.Request(
            f"{base}/api/items",
            data=_json.dumps({"title": "buy milk"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=3)
        with urllib.request.urlopen(f"{base}/api/items", timeout=3) as resp:
            body = resp.read().decode(errors="replace")
        return ("buy milk" in body, "ok" if "buy milk" in body else "posted item not in GET list")
    except Exception as exc:
        return False, f"api error: {exc}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


def _setup_refactor(workdir: Path) -> None:
    """Seed a buggy module + a failing test so Codex has something to fix."""
    (workdir / "mathutils.py").write_text(
        "def average(nums):\n"
        "    # BUG: integer division + no empty-list guard\n"
        "    return sum(nums) // len(nums)\n"
    )
    (workdir / "tests.py").write_text(
        "from mathutils import average\n"
        "assert average([1, 2]) == 1.5, average([1, 2])\n"
        "assert average([]) == 0\n"
        "print('ok')\n"
    )


@dataclass
class E2ECase:
    name: str
    prompt: str  # may reference {path} and {port}
    verify: Callable[[Path, int], tuple[bool, str]]
    tier: int = 1
    needs_port: bool = False
    setup: Callable[[Path], None] | None = None


E2E_CASES: list[E2ECase] = [
    E2ECase(
        name="e2e_add_function",
        tier=1,
        prompt=(
            "Use the codex subagent to implement a function add(a, b) that returns "
            "their sum, written to a file calc.py in {path}."
        ),
        verify=_verify_add_function,
    ),
    E2ECase(
        name="e2e_static_site",
        tier=2,
        prompt=(
            "Use the codex subagent to build a basic static website in {path}: an "
            "index.html with a <title>My Site</title> and an <h1> heading, plus a "
            "styles.css (linked from index.html) that sets the page background color. "
            "Keep it to plain HTML/CSS."
        ),
        verify=_verify_static_site,
    ),
    E2ECase(
        name="e2e_running_web_server",
        tier=2,
        prompt=(
            "Use the codex subagent to build AND start a basic website in {path}. "
            "Create server.py using only the Python standard library that serves an "
            "index.html whose body contains the exact text 'Hello from Codex' at "
            "http://localhost:{port}/. Running `python server.py` must start the "
            "server listening on port {port}. Build it, start it briefly to confirm "
            "it responds with the page, then stop it and report what you did."
        ),
        verify=_verify_running_server,
        needs_port=True,
    ),
    E2ECase(
        name="e2e_json_api",
        tier=3,
        prompt=(
            "Use the codex subagent to build a minimal JSON todo API in {path} using "
            "only the Python standard library. Create server.py exposing GET "
            "/api/items (returns a JSON array of items) and POST /api/items (accepts "
            "{{\"title\": ...}} and appends it). Running `python server.py` must listen "
            "on port {port}. Persist items in memory is fine."
        ),
        verify=_verify_json_api,
        needs_port=True,
    ),
    E2ECase(
        name="e2e_cli_with_tests",
        tier=3,
        prompt=(
            "Use the codex subagent to build a small word-count CLI in {path}: a file "
            "wordcount.py with a function count_words(text) returning the number of "
            "whitespace-separated words, and a test file tests.py (standard library "
            "only) that asserts several cases and prints 'ok'. Running `python "
            "tests.py` must exit 0."
        ),
        verify=_verify_tests_pass,
    ),
    E2ECase(
        name="e2e_refactor_passes_tests",
        tier=4,
        prompt=(
            "The directory {path} has mathutils.py with a buggy average() and a "
            "tests.py that currently fails. Use the codex subagent to fix "
            "mathutils.py so that `python tests.py` passes (float division, and "
            "average([]) returns 0). Do not weaken the tests."
        ),
        verify=_verify_tests_pass,
        setup=_setup_refactor,
    ),
]


def eval_e2e_case(case: E2ECase, k: int, threshold: float) -> CaseResult:
    """Route a multi-step build task through the main agent to the REAL codex
    tool, then independently verify the artifacts (and, where relevant, that the
    result actually runs). Bounded by the global BUDGET so an overnight loop
    can't run away."""
    import tempfile

    k = min(k, int(os.getenv("EVAL_E2E_K", "3")))
    passes, notes, runs_done = 0, [], 0
    for i in range(k):
        ok_budget, why = BUDGET.available()
        if not ok_budget:
            notes.append(f"stopped early: {why} (after {runs_done} runs)")
            break
        BUDGET.record()
        workdir = Path(tempfile.mkdtemp(prefix=f"{case.name}_"))
        if case.setup is not None:
            try:
                case.setup(workdir)
            except Exception as exc:
                notes.append(f"run {i}: setup failed: {exc}")
                continue
        port = _free_port() if case.needs_port else 0
        prompt = case.prompt.format(path=workdir, port=port)
        try:
            # Isolate to workdir (same as the multi-turn case): otherwise the
            # agent inspects the SHARED workspace, sees stale artifacts from a
            # prior case, concludes the task is already done, and never delegates
            # — leaving the fresh workdir empty and verification failing.
            with _chat_files_dir(workdir):
                calls, _ = _run_agent(prompt, record_sink=None)
            ok, why2 = case.verify(workdir, port)
            if not ok:
                results = [c.get("result") for c in calls]
                why2 = f"{why2}; codex_calls={len(calls)} results={results}"
        except Exception as exc:
            ok, why2 = False, f"exception: {exc}"
        runs_done += 1
        if ok:
            passes += 1
        else:
            notes.append(f"run {i}: {why2} (workdir={workdir})")
    if runs_done == 0:
        return CaseResult(case.name, "e2e-success", 0, 0, 0.0, threshold, False,
                          notes or ["no runs executed"], tier=case.tier, skipped=True)
    score = passes / runs_done
    return CaseResult(case.name, "e2e-success", runs_done, passes, score, threshold,
                      score >= threshold, notes, tier=case.tier)


# --------------------------------------------------------------------------- #
# Multi-turn cases: an LLM plays the human, driving a real conversation with the
# main agent over several turns. Each turn the agent may re-delegate to the REAL
# Codex, building on prior work. Tests conversational continuity + repeated,
# correct delegation — not just a single hand-off.
# --------------------------------------------------------------------------- #

_SIMULATOR_PROMPT = """You are role-playing a HUMAN user chatting with an AI coding assistant.

Your overall GOAL for the whole conversation:
{goal}

Conversation so far (oldest first; may be empty):
{transcript}

Write ONLY your next message to the assistant: a short, natural user turn that
pushes toward the GOAL, building on what has already been done. Ask for one
increment at a time (like a real user). When your GOAL says the coding subagent
should do the work, make each build/change request explicit about it — e.g.
"use your codex coding subagent to ..." — so the assistant delegates the coding
rather than hand-editing it itself. If the GOAL is already fully accomplished,
reply with exactly: DONE
"""


def _simulate_user_turn(goal: str, messages: list[dict]) -> str | None:
    """Ask the simulator LLM for the next human message, or None when it says DONE."""
    from openai import OpenAI

    transcript = "\n".join(f"{m['role']}: {str(m['content'])[:600]}" for m in messages) or "(none yet)"
    resp = OpenAI().chat.completions.create(
        model=MODEL_ID,
        messages=[{"role": "user", "content": _SIMULATOR_PROMPT.format(goal=goal, transcript=transcript)}],
        temperature=0.3,
    )
    text = (resp.choices[0].message.content or "").strip()
    if text.upper().startswith("DONE") or not text:
        return None
    return text


def _verify_multiroute_server(workdir: Path, port: int) -> tuple[bool, str]:
    """Start the server the agent had Codex build across turns and confirm it
    serves BOTH routes with cross-linking nav."""
    import subprocess
    import time
    import urllib.request

    if not (workdir / "server.py").exists():
        return False, "server.py missing"
    proc = subprocess.Popen(
        ["python", "server.py"], cwd=str(workdir),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        home = None
        for _ in range(30):
            if proc.poll() is not None:
                return False, f"server exited early (rc={proc.returncode})"
            try:
                with urllib.request.urlopen(f"{base}/", timeout=1) as r:
                    home = r.read().decode(errors="replace")
                    break
            except Exception:
                time.sleep(0.2)
        else:
            return False, f"server did not respond on / (port {port})"
        try:
            with urllib.request.urlopen(f"{base}/about", timeout=2) as r:
                about = r.read().decode(errors="replace")
        except Exception as exc:
            return False, f"/about route not served: {exc}"
        if "/about" not in home.lower():
            return False, "home page nav missing link to /about"
        al = about.lower()
        if 'href="/"' not in al and "home" not in al:
            return False, "about page nav missing link back home"
        return True, "ok"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


@dataclass
class MultiTurnCase:
    name: str
    goal: str  # references {path} and optionally {port}
    verify: Callable[[Path, int], tuple[bool, str]]
    tier: int = 5
    needs_port: bool = False
    max_turns: int = 4


MULTITURN_CASES: list[MultiTurnCase] = [
    MultiTurnCase(
        name="mt_multiroute_server",
        tier=5,
        max_turns=5,
        needs_port=True,
        goal=(
            "Build a Python web application in {path} using only the standard "
            "library — this is real backend coding, so have the coding subagent do "
            "it. Create server.py that serves a home page at / and listens on port "
            "{port} when run as `python server.py`. Then, in later messages, ask to "
            "add an /about route and a shared nav bar linking Home (/) and About "
            "(/about) on both pages. Build it up one route at a time."
        ),
        verify=_verify_multiroute_server,
    ),
]


def eval_multiturn_case(case: MultiTurnCase, k: int, threshold: float) -> CaseResult:
    """Drive a simulated multi-turn conversation; each turn rebuilds the agent
    with the full history (like the box runtime) and may hit the REAL Codex.
    Passes when the final artifacts verify AND Codex was actually delegated to."""
    import tempfile

    k = min(k, int(os.getenv("EVAL_MULTITURN_K", "2")))
    passes, notes, runs_done = 0, [], 0
    for i in range(k):
        ok_budget, why = BUDGET.available()
        if not ok_budget:
            notes.append(f"stopped early: {why} (after {runs_done} runs)")
            break
        workdir = Path(tempfile.mkdtemp(prefix=f"{case.name}_"))
        port = _free_port() if case.needs_port else 0
        goal = case.goal.format(path=workdir, port=port)
        messages: list[dict] = []
        calls_per_turn: list[int] = []
        with _chat_files_dir(workdir):
            for _turn in range(case.max_turns):
                ok_b, _ = BUDGET.available()
                if not ok_b:
                    break
                user_msg = _simulate_user_turn(goal, messages)
                if user_msg is None:
                    break
                messages.append({"role": "user", "content": user_msg})
                BUDGET.record()
                try:
                    # Fresh agent + full history each turn; record_sink=None -> real codex.
                    agent = _build_agent(None)
                    calls, text = _run_messages(agent, messages)
                except Exception as exc:
                    notes.append(f"run {i}: turn error: {exc}")
                    calls, text = [], "(error)"
                calls_per_turn.append(len(calls))
                messages.append({"role": "assistant", "content": text or "(no text)"})
        runs_done += 1
        try:
            ok, why2 = case.verify(workdir, port)
        except Exception as exc:
            ok, why2 = False, f"verify error: {exc}"
        delegated = any(c > 0 for c in calls_per_turn)
        if ok and delegated:
            passes += 1
        else:
            notes.append(
                f"run {i}: verify={ok} ({why2}); codex_calls_per_turn={calls_per_turn} "
                f"(workdir={workdir})"
            )
    if runs_done == 0:
        return CaseResult(case.name, "multiturn", 0, 0, 0.0, threshold, False,
                          notes or ["no runs executed"], tier=case.tier, skipped=True)
    score = passes / runs_done
    return CaseResult(case.name, "multiturn", runs_done, passes, score, threshold,
                      score >= threshold, notes, tier=case.tier)


# Cheap behavior cases (stubbed Codex) — always run, always tier 1.
STUB_CASES: list[tuple[str, Callable[[int, float], CaseResult]]] = [
    ("called_appropriately", eval_called_appropriately),
    ("no_over_delegation", eval_no_over_delegation),
    ("instruction_quality", eval_instruction_quality),
]


def run_evals(
    k: int = DEFAULT_K,
    threshold: float = DEFAULT_THRESHOLD,
    only: set[str] | None = None,
    max_tier: int = DEFAULT_MAX_TIER,
) -> dict:
    results: list[CaseResult] = []

    for name, fn in STUB_CASES:
        if only and name not in only:
            continue
        print(f"[eval] {name} (k={k}) ...", flush=True)
        try:
            res = fn(k, threshold)
        except Exception as exc:  # keep going per "skip + log" policy
            res = CaseResult(name, "?", k, 0, 0.0, threshold, False, [f"ERROR: {exc}"])
        results.append(res)
        print(f"       -> {'PASS' if res.passed else 'FAIL'} score={res.score:.2f}", flush=True)

    for case in E2E_CASES:
        explicit = bool(only and case.name in only)
        if only and not explicit:
            continue
        # Tier gate ("as the night progresses"): skip harder tiers unless the
        # loop has raised max_tier, or the case was named explicitly.
        if not explicit and case.tier > max_tier:
            results.append(CaseResult(case.name, "e2e-success", 0, 0, 0.0, threshold,
                                      False, [f"skipped: tier {case.tier} > max_tier {max_tier}"],
                                      tier=case.tier, skipped=True))
            print(f"[eval] {case.name} — SKIP (tier {case.tier} > {max_tier})", flush=True)
            continue
        ok_budget, why = BUDGET.available()
        if not ok_budget:
            results.append(CaseResult(case.name, "e2e-success", 0, 0, 0.0, threshold,
                                      False, [f"skipped: {why}"], tier=case.tier, skipped=True))
            print(f"[eval] {case.name} — SKIP ({why})", flush=True)
            continue
        print(f"[eval] {case.name} (tier {case.tier}, e2e_runs={BUDGET.e2e_runs}/{MAX_E2E_RUNS}) ...", flush=True)
        try:
            res = eval_e2e_case(case, k, threshold)
        except Exception as exc:
            res = CaseResult(case.name, "e2e-success", 0, 0, 0.0, threshold, False,
                             [f"ERROR: {exc}"], tier=case.tier)
        results.append(res)
        print(f"       -> {'PASS' if res.passed else 'FAIL'} score={res.score:.2f}", flush=True)

    for case in MULTITURN_CASES:
        explicit = bool(only and case.name in only)
        if only and not explicit:
            continue
        if not explicit and case.tier > max_tier:
            results.append(CaseResult(case.name, "multiturn", 0, 0, 0.0, threshold,
                                      False, [f"skipped: tier {case.tier} > max_tier {max_tier}"],
                                      tier=case.tier, skipped=True))
            print(f"[eval] {case.name} — SKIP (tier {case.tier} > {max_tier})", flush=True)
            continue
        ok_budget, why = BUDGET.available()
        if not ok_budget:
            results.append(CaseResult(case.name, "multiturn", 0, 0, 0.0, threshold,
                                      False, [f"skipped: {why}"], tier=case.tier, skipped=True))
            print(f"[eval] {case.name} — SKIP ({why})", flush=True)
            continue
        print(f"[eval] {case.name} (multi-turn, tier {case.tier}, e2e_runs={BUDGET.e2e_runs}/{MAX_E2E_RUNS}) ...", flush=True)
        try:
            res = eval_multiturn_case(case, k, threshold)
        except Exception as exc:
            res = CaseResult(case.name, "multiturn", 0, 0, 0.0, threshold, False,
                             [f"ERROR: {exc}"], tier=case.tier)
        results.append(res)
        print(f"       -> {'PASS' if res.passed else 'FAIL'} score={res.score:.2f}", flush=True)

    scored = [r for r in results if not r.skipped]
    scorecard = {
        "model": MODEL_ID,
        "k": k,
        "threshold": threshold,
        "max_tier": max_tier,
        "budget": {
            "e2e_runs": BUDGET.e2e_runs,
            "max_e2e_runs": MAX_E2E_RUNS,
            "minutes_elapsed": round((monotonic() - BUDGET.start) / 60, 1),
            "max_minutes": MAX_MINUTES,
        },
        "all_passed": bool(scored) and all(r.passed for r in scored),
        "cases": [r.__dict__ for r in results],
    }
    out = Path(__file__).parent / "scorecard.json"
    out.write_text(json.dumps(scorecard, indent=2))
    print(f"[eval] wrote {out} — all_passed={scorecard['all_passed']} "
          f"(e2e_runs={BUDGET.e2e_runs}/{MAX_E2E_RUNS})")
    return scorecard


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    only = set(args) or None
    card = run_evals(only=only)
    sys.exit(0 if card["all_passed"] else 1)
