import os
import re
import subprocess
import glob as globlib
import importlib.util
import threading
import json
from urllib import error as urlerror
from urllib import request as urlrequest
from .workspace import ensure_workspace_dir, resolve_workspace_path

RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
_SUBAGENT_FN = None
_SUBAGENT_LOCK = threading.Lock()
_WORKSPACE_DIR = ensure_workspace_dir()


def read(path: str, offset: int = 0, limit: int = None):
    resolved = resolve_workspace_path(path)
    try:
        lines = open(resolved).readlines()
    except FileNotFoundError:
        return f"error: file not found: {resolved}"
    except IsADirectoryError:
        return f"error: path is a directory: {resolved}"
    if limit is None:
        limit = len(lines)
    selected = lines[offset : offset + limit]
    return "".join(f"{offset + idx + 1:4}| {line}" for idx, line in enumerate(selected))


def write(path: str, content: str):
    resolved = resolve_workspace_path(path)
    os.makedirs(os.path.dirname(str(resolved)), exist_ok=True)
    with open(resolved, "w") as f:
        f.write(content)
    return "ok"


def edit(path: str, old: str, new: str, all: bool = False):
    resolved = resolve_workspace_path(path)
    text = open(resolved).read()
    if old not in text:
        return "error: old_string not found"
    count = text.count(old)
    if not all and count > 1:
        return f"error: old_string appears {count} times, must be unique (use all=true)"
    replacement = text.replace(old, new) if all else text.replace(old, new, 1)
    with open(resolved, "w") as f:
        f.write(replacement)
    return "ok"


def glob(pat: str, path: str = "."):
    resolved_path = str(resolve_workspace_path(path))
    pattern = (resolved_path + "/" + pat).replace("//", "/")
    files = globlib.glob(pattern, recursive=True)
    files = sorted(
        files,
        key=lambda f: os.path.getmtime(f) if os.path.isfile(f) else 0,
        reverse=True,
    )
    return "\n".join(files) or "none"


def grep(pat: str, path: str = "."):
    pattern = re.compile(pat)
    hits = []
    resolved_path = str(resolve_workspace_path(path))
    for filepath in globlib.glob(resolved_path + "/**", recursive=True):
        try:
            for line_num, line in enumerate(open(filepath), 1):
                if pattern.search(line):
                    hits.append(f"{filepath}:{line_num}:{line.rstrip()}")
        except Exception:
            pass
    return "\n".join(hits[:50]) or "none"


def bash(cmd: str, timeout: float = 30):
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=float(timeout), stdin=subprocess.DEVNULL,
            cwd=str(_WORKSPACE_DIR),
        )
        out = (result.stdout + result.stderr).strip()
    except subprocess.TimeoutExpired as e:
        out = ((e.stdout or "") + (e.stderr or "")).strip()
        out += f"\n(timed out after {timeout:g}s)"
    if out:
        print(f"  {DIM}│ {out}{RESET}", flush=True)
    return out or "(empty)"


def _get_cron_manager():
    # Imported lazily to avoid package import cycles.
    from .crons import cron_manager
    return cron_manager


def cron(action: str, name: str = None, description: str = None,
         instructions: str = None, schedule: str = None, cron_id: str = None):
    cron_manager = _get_cron_manager()
    if action == "create":
        if not all([name, description, instructions, schedule]):
            return "error: create requires name, description, instructions, and schedule"
        try:
            cid = cron_manager.create_cron(name, description, instructions, schedule)
            return f"Cron created: {cid[:8]} ({name})"
        except ValueError as e:
            return f"error: invalid schedule -- {e}"

    elif action == "list":
        return cron_manager.list_crons()

    elif action == "edit":
        if not cron_id:
            return "error: edit requires cron_id"
        try:
            return cron_manager.edit_cron(
                cron_id, name=name, description=description,
                instructions=instructions, schedule=schedule,
            )
        except ValueError as e:
            return f"error: invalid schedule -- {e}"

    elif action == "delete":
        if not cron_id:
            return "error: delete requires cron_id"
        return cron_manager.delete_cron(cron_id)

    else:
        return f"error: unknown action '{action}'. Use create, list, edit, or delete."


def _load_subagent_tool():
    global _SUBAGENT_FN
    if _SUBAGENT_FN is not None:
        return _SUBAGENT_FN

    with _SUBAGENT_LOCK:
        if _SUBAGENT_FN is None:
            file_path = os.path.join(os.path.dirname(__file__), "tools", "subagent.py")
            spec = importlib.util.spec_from_file_location("aios_core._subagent_tool", file_path)
            if spec is None or spec.loader is None:
                raise RuntimeError(f"could not load subagent tool from {file_path}")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _SUBAGENT_FN = module.subagent
    return _SUBAGENT_FN


def subagent(task: str = None, timeout: float = 60):
    try:
        fn = _load_subagent_tool()
        return fn(task=task, timeout=timeout)
    except Exception as e:
        return f"error: subagent unavailable -- {e}"


def codex(
    task: str = None,
    timeout: float = 180,
    model: str = None,
    sandbox: str = "workspace-write",
    path: str = ".",
):
    """
    Delegate a task to Codex CLI in non-interactive mode.
    """
    if not isinstance(task, str) or not task.strip():
        return "error: task is required"

    try:
        timeout_value = float(timeout)
    except (TypeError, ValueError):
        return "error: timeout must be a number"
    if timeout_value <= 0:
        return "error: timeout must be > 0"

    allowed_sandbox = {"read-only", "workspace-write", "danger-full-access"}
    if sandbox not in allowed_sandbox:
        return "error: sandbox must be one of read-only, workspace-write, danger-full-access"

    if not isinstance(path, str) or not path.strip():
        return "error: path must be a non-empty string"
    workdir = resolve_workspace_path(path.strip())
    if not workdir.exists():
        return f"error: path does not exist: {workdir}"
    if not workdir.is_dir():
        return f"error: path is not a directory: {workdir}"

    cmd = [
        "codex", "exec",
        "--skip-git-repo-check",
        "--color", "never",
        "--sandbox", sandbox,
        "--cd", str(workdir),
        "-",
    ]
    if isinstance(model, str) and model.strip():
        cmd.extend(["--model", model.strip()])

    try:
        result = subprocess.run(
            cmd,
            input=task.strip(),
            capture_output=True,
            text=True,
            timeout=timeout_value,
            cwd=str(workdir),
        )
    except FileNotFoundError:
        return "error: codex CLI is not installed or not on PATH"
    except subprocess.TimeoutExpired as e:
        partial = ((e.stdout or "") + (e.stderr or "")).strip()
        if partial:
            return f"{partial}\n(error: codex timed out after {timeout_value:g}s)"
        return f"error: codex timed out after {timeout_value:g}s"
    except Exception as e:
        return f"error: codex failed -- {e}"

    out = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        if out:
            return f"error: codex exit {result.returncode} -- {out}"
        return f"error: codex exit {result.returncode}"
    return out or "(empty)"


def tavily_search(
    query: str = None,
    search_depth: str = "basic",
    max_results: int = 5,
    topic: str = "general",
    include_answer: bool = True,
    include_raw_content: bool = False,
    include_domains: list = None,
    exclude_domains: list = None,
    time_range: str = None,
    timeout: float = 20,
):
    """
    Run a Tavily web search using TAVILY_API_KEY from env.
    Returns the raw Tavily JSON response as a formatted string.
    """
    if not isinstance(query, str) or not query.strip():
        return "error: query is required"

    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        return "error: TAVILY_API_KEY is not set"

    try:
        timeout_value = float(timeout)
    except (TypeError, ValueError):
        return "error: timeout must be a number"
    if timeout_value <= 0:
        return "error: timeout must be > 0"

    payload = {
        "query": query.strip(),
        "search_depth": search_depth,
        "max_results": max_results,
        "topic": topic,
        "include_answer": include_answer,
        "include_raw_content": include_raw_content,
    }
    if include_domains:
        payload["include_domains"] = include_domains
    if exclude_domains:
        payload["exclude_domains"] = exclude_domains
    if time_range:
        payload["time_range"] = time_range

    body = json.dumps(payload).encode("utf-8")
    req = urlrequest.Request(
        "https://api.tavily.com/search",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    try:
        with urlrequest.urlopen(req, timeout=timeout_value) as resp:
            raw = resp.read().decode("utf-8")
    except urlerror.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(detail)
            error_msg = parsed.get("detail", {}).get("error") or parsed.get("error") or detail
        except json.JSONDecodeError:
            error_msg = detail
        return f"error: Tavily HTTP {e.code} -- {error_msg}"
    except urlerror.URLError as e:
        return f"error: Tavily request failed -- {e.reason}"
    except Exception as e:
        return f"error: Tavily request failed -- {e}"

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    return json.dumps(parsed, indent=2, ensure_ascii=True)