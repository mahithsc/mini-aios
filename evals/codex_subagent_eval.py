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
from typing import Callable

from agno.agent import Agent
from agno.models.openai import OpenAIChat
from agno.run.agent import RunEvent as AgentRunEvent

DEFAULT_K = int(os.getenv("EVAL_K", "5"))
DEFAULT_THRESHOLD = float(os.getenv("EVAL_THRESHOLD", "0.8"))
MODEL_ID = os.getenv("AIOS_MODEL_ID", "gpt-4.1")


# --------------------------------------------------------------------------- #
# Agent construction with a recording stub for codex_subagent
# --------------------------------------------------------------------------- #


def _make_recording_codex(sink: list[dict]) -> Callable:
    """A stand-in for codex_subagent that records the args the agent passes and
    returns a canned success — so decision/instruction cases don't pay for a
    real Codex run. Mirrors the real tool's name + docstring + signature so the
    model's view of the tool is identical."""
    from aios_core.tools.codex_subagent import codex_subagent as real

    def codex_subagent(task=None, timeout=180, model=None, path=".", fc=None):
        sink.append({"task": task, "timeout": timeout, "model": model, "path": path})
        return "Completed the delegated task. (recording stub)"

    codex_subagent.__doc__ = real.__doc__
    return codex_subagent


def _build_agent(record_sink: list[dict] | None) -> Agent:
    from aios_core.agent import MAIN_TOOLS, _build_prompt, DEFAULT_MODEL_ID

    tools = []
    for tool in MAIN_TOOLS:
        if getattr(tool, "__name__", "") == "codex_subagent" and record_sink is not None:
            tools.append(_make_recording_codex(record_sink))
        else:
            tools.append(tool)
    return Agent(
        system_message=_build_prompt(include_subagent_tool=True),
        tools=tools,
        model=OpenAIChat(id=DEFAULT_MODEL_ID),
    )


def _run_agent(prompt: str, record_sink: list[dict] | None) -> tuple[list[dict], str]:
    """Run the agent once; return (codex_calls, final_text)."""
    agent = _build_agent(record_sink)
    calls: list[dict] = []
    final_chunks: list[str] = []
    for event in agent.run([{"role": "user", "content": prompt}], stream=True, stream_events=True):
        if event.event == AgentRunEvent.tool_call_started and event.tool is not None:
            if event.tool.tool_name == "codex_subagent":
                calls.append({"args": event.tool.tool_args})
        elif event.event == AgentRunEvent.run_content and event.content is not None:
            final_chunks.append(str(event.content))
    return calls, "".join(final_chunks)


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
{user_request}

DELEGATED TASK:
{delegated_task}
"""


def _judge_instruction(user_request: str, delegated_task: str) -> float:
    from openai import OpenAI

    client = OpenAI()
    prompt = _JUDGE_RUBRIC.format(user_request=user_request, delegated_task=delegated_task or "(none)")
    resp = client.chat.completions.create(
        model=MODEL_ID,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
    )
    try:
        data = json.loads(resp.choices[0].message.content.strip())
        return float(data.get("score", 0.0))
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


POSITIVE_PROMPT = (
    "Use the codex subagent to implement a function add(a, b) that returns their "
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
    scores, notes = [], []
    for i in range(k):
        sink: list[dict] = []
        _run_agent(POSITIVE_PROMPT, sink)
        if not sink:
            notes.append(f"run {i}: no delegation to judge")
            scores.append(0.0)
            continue
        task = sink[-1].get("task") or ""
        s = _judge_instruction(POSITIVE_PROMPT, task)
        scores.append(s)
        if s < threshold:
            notes.append(f"run {i}: weak task (score {s:.2f}): {task[:80]!r}")
    avg = sum(scores) / len(scores) if scores else 0.0
    return CaseResult("instruction_quality", "correct-instructions", k, sum(1 for s in scores if s >= threshold), avg, threshold, avg >= threshold, notes)


def eval_delegation_succeeds(k: int, threshold: float) -> CaseResult:
    """End-to-end with the REAL codex tool. Runs each attempt in a fresh temp
    workdir; asserts calc.py is created with an add function."""
    import tempfile

    # The real-Codex e2e case is the expensive one; bound its runs independently
    # of the cheap stubbed cases (default 3, override with EVAL_E2E_K).
    k = min(k, int(os.getenv("EVAL_E2E_K", "3")))
    passes, notes = 0, []
    for i in range(k):
        workdir = Path(tempfile.mkdtemp(prefix="codex_eval_"))
        os.environ["AIOS_EVAL_WORKDIR"] = str(workdir)
        # Real tool: pass record_sink=None so the actual codex_subagent runs.
        _, _final = _run_agent(
            POSITIVE_PROMPT + f" Use path {workdir}.", record_sink=None
        )
        calc = workdir / "calc.py"
        ok = calc.exists() and "def add" in calc.read_text() if calc.exists() else False
        if ok:
            passes += 1
        else:
            notes.append(f"run {i}: artifact missing/invalid in {workdir}")
    score = passes / k
    return CaseResult("delegation_succeeds", "e2e-success", k, passes, score, threshold, score >= threshold, notes)


CASES: list[tuple[str, Callable[[int, float], CaseResult]]] = [
    ("called_appropriately", eval_called_appropriately),
    ("no_over_delegation", eval_no_over_delegation),
    ("instruction_quality", eval_instruction_quality),
    ("delegation_succeeds", eval_delegation_succeeds),
]


def run_evals(k: int = DEFAULT_K, threshold: float = DEFAULT_THRESHOLD, only: set[str] | None = None) -> dict:
    results = []
    for name, fn in CASES:
        if only and name not in only:
            continue
        print(f"[eval] {name} (k={k}) ...", flush=True)
        try:
            res = fn(k, threshold)
        except Exception as exc:  # keep going per "skip + log" policy
            res = CaseResult(name, "?", k, 0, 0.0, threshold, False, [f"ERROR: {exc}"])
        results.append(res)
        print(f"       -> {'PASS' if res.passed else 'FAIL'} score={res.score:.2f}", flush=True)

    scorecard = {
        "model": MODEL_ID,
        "k": k,
        "threshold": threshold,
        "all_passed": all(r.passed for r in results),
        "cases": [r.__dict__ for r in results],
    }
    out = Path(__file__).parent / "scorecard.json"
    out.write_text(json.dumps(scorecard, indent=2))
    print(f"[eval] wrote {out} — all_passed={scorecard['all_passed']}")
    return scorecard


if __name__ == "__main__":
    only = set(sys.argv[1:]) or None
    card = run_evals(only=only)
    sys.exit(0 if card["all_passed"] else 1)
