"""Public exposure via cloudflared (step 4b).

Two modes:

- **quick tunnel** (``start_quick``): ``cloudflared tunnel --url`` → an ephemeral
  ``https://<rand>.trycloudflare.com`` URL. No account/auth needed. Used to prove
  real public reachability and as a zero-config fallback.
- **named tunnel** (``start_named``, TODO): one durable tunnel per box fronting the
  reverse proxy, with wildcard DNS ``*.apps.<zone>`` so each app gets
  ``https://<slug>.apps.<zone>``. Requires a VALID Cloudflare API token (the one
  currently in .env returns "Invalid API Token") + cloudflared auth. Blocked until
  a working token is supplied.

The manager only runs cloudflared and parses its output; the reverse proxy does the
per-slug routing behind it.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time

_QUICK_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def cloudflared_available() -> bool:
    return shutil.which("cloudflared") is not None


class TunnelError(RuntimeError):
    pass


class TunnelManager:
    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self.public_url: str | None = None

    def start_quick(self, local_url: str, timeout: float = 40) -> str:
        """Start an ephemeral quick tunnel to ``local_url``; return the public URL."""
        if not cloudflared_available():
            raise TunnelError("cloudflared is not installed")
        self._proc = subprocess.Popen(
            ["cloudflared", "tunnel", "--url", local_url, "--no-autoupdate"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        deadline = time.time() + timeout
        assert self._proc.stdout is not None
        while time.time() < deadline:
            line = self._proc.stdout.readline()
            if line == "" and self._proc.poll() is not None:
                break
            match = _QUICK_URL_RE.search(line)
            if match:
                self.public_url = match.group(0)
                threading.Thread(target=self._drain, daemon=True).start()
                return self.public_url
        self.stop()
        raise TunnelError("cloudflared did not produce a quick tunnel URL in time")

    def _drain(self) -> None:
        try:
            assert self._proc is not None and self._proc.stdout is not None
            for _ in self._proc.stdout:
                pass
        except Exception:
            pass

    def is_up(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def stop(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except Exception:
                self._proc.kill()
        self.public_url = None
