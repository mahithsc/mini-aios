from concurrent.futures import ThreadPoolExecutor, TimeoutError
import os


_POOL = ThreadPoolExecutor(max_workers=max(4, (os.cpu_count() or 1) * 2))


def _run_single_task(task: str) -> str:
    from aios_core.agent import create_subagent_worker

    prompt = f"""\
You are a delegated subagent. Execute the task below completely.

Task:
{task}

Rules:
- You should focus on execution, not instructions for someone else.
- Do not ask follow up questions. The caller cannot answer.
- Return concise, directly useful output.
"""
    agent = create_subagent_worker()
    response = agent.run([{"role": "user", "content": prompt}])
    return (response.content or "").strip() or "(empty)"


def subagent(task: str = None, timeout: float = 60):
    """
    Run delegated subagent work synchronously.
    Use one call per delegated task.
    """
    if timeout is None or float(timeout) <= 0:
        return "error: timeout must be > 0"

    if not isinstance(task, str) or not task.strip():
        return "error: task is required"

    future = _POOL.submit(_run_single_task, task.strip())
    try:
        return future.result(timeout=float(timeout))
    except TimeoutError:
        return f"error: subagent timed out after {float(timeout):g}s"
    except Exception as e:
        return f"error: subagent failed -- {e}"
