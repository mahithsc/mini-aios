from html.parser import HTMLParser
from urllib import error as urlerror
from urllib import request as urlrequest

_MAX_CHARS = 50_000


class _HTMLToText(HTMLParser):
    """Minimal HTML-to-text converter using only the stdlib."""

    _SKIP_TAGS = frozenset({"script", "style", "svg", "noscript", "head"})
    _BLOCK_TAGS = frozenset({
        "p", "div", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6",
        "li", "tr", "blockquote", "pre", "section", "article", "header",
        "footer", "nav", "main", "aside", "details", "summary", "figcaption",
    })

    def __init__(self):
        super().__init__()
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data):
        if self._skip_depth:
            return
        self._parts.append(data)

    def get_text(self) -> str:
        raw = "".join(self._parts)
        # Collapse runs of blank lines / whitespace into readable text
        lines = (line.strip() for line in raw.splitlines())
        prev_blank = False
        out: list[str] = []
        for line in lines:
            if not line:
                if not prev_blank:
                    out.append("")
                prev_blank = True
            else:
                out.append(line)
                prev_blank = False
        return "\n".join(out).strip()


def _html_to_text(html: str) -> str:
    parser = _HTMLToText()
    parser.feed(html)
    return parser.get_text()


def fetch(url: str = None, timeout: float = 30):
    """
    Fetch a web page and return its contents as readable text.
    HTML is automatically converted to plain text.
    """
    if not isinstance(url, str) or not url.strip():
        return "error: url is required"

    url = url.strip()
    if not url.startswith(("http://", "https://")):
        return "error: url must start with http:// or https://"

    try:
        timeout_value = float(timeout)
    except (TypeError, ValueError):
        return "error: timeout must be a number"
    if timeout_value <= 0:
        return "error: timeout must be > 0"

    req = urlrequest.Request(
        url,
        headers={"User-Agent": "AIOS-Fetch/1.0"},
        method="GET",
    )

    try:
        with urlrequest.urlopen(req, timeout=timeout_value) as resp:
            content_type = resp.headers.get("Content-Type", "")
            raw = resp.read().decode("utf-8", errors="replace")
    except urlerror.HTTPError as e:
        return f"error: HTTP {e.code} fetching {url}"
    except urlerror.URLError as e:
        return f"error: request failed -- {e.reason}"
    except Exception as e:
        return f"error: request failed -- {e}"

    if "html" in content_type.lower():
        text = _html_to_text(raw)
    else:
        text = raw

    if len(text) > _MAX_CHARS:
        text = text[:_MAX_CHARS] + f"\n\n[truncated at {_MAX_CHARS} characters]"

    return text
