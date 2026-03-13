import json
import os
from urllib import error as urlerror
from urllib import request as urlrequest


def tavily_search(
    query: str = None,
    search_depth: str = "basic",
    max_results: int = 5,
    topic: str = "general",
    include_answer: bool = True,
    include_raw_content: bool = False,
    include_domains: list[str] | None = None,
    exclude_domains: list[str] | None = None,
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
