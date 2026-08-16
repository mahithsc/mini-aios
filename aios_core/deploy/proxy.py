"""Reverse proxy for public app exposure (step 4a).

One box exposes one HTTP endpoint (later fronted by a single cloudflared tunnel).
This proxy routes each request to the right app container by the request's Host
header: ``<slug>.apps.<zone>`` → the loopback port that slug's container publishes.
The Supervisor registers a route on start and removes it on stop.

Routing only — the containers still do the work. Kept dependency-free (stdlib
http.server) so it's trivially testable against real backend servers.
"""

from __future__ import annotations

import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class RouteRegistry:
    def __init__(self) -> None:
        self._routes: dict[str, int] = {}
        self._lock = threading.Lock()

    def register(self, slug: str, host_port: int) -> None:
        with self._lock:
            self._routes[slug] = host_port

    def unregister(self, slug: str) -> None:
        with self._lock:
            self._routes.pop(slug, None)

    def get(self, slug: str) -> int | None:
        with self._lock:
            return self._routes.get(slug)

    def slugs(self) -> list[str]:
        with self._lock:
            return list(self._routes)


def slug_from_host(host: str, apps_domain: str) -> str | None:
    """`one.apps.winkapiserver.org[:port]` with apps_domain=`apps.winkapiserver.org` -> `one`."""
    host = (host or "").split(":")[0].strip().lower()
    suffix = "." + apps_domain.strip().lower()
    if not host.endswith(suffix):
        return None
    label = host[: -len(suffix)]
    if not label or "." in label:  # exactly one leading label
        return None
    return label


def _make_handler(registry: RouteRegistry, apps_domain: str):
    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):  # quiet
            pass

        def _proxy(self):
            slug = slug_from_host(self.headers.get("Host", ""), apps_domain)
            port = registry.get(slug) if slug else None
            if port is None:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(length) if length else None
            target = f"http://127.0.0.1:{port}{self.path}"
            req = urllib.request.Request(target, data=body, method=self.command)
            ct = self.headers.get("Content-Type")
            if ct:
                req.add_header("Content-Type", ct)
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = resp.read()
                    self.send_response(resp.status)
                    rct = resp.headers.get("Content-Type")
                    if rct:
                        self.send_header("Content-Type", rct)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
            except urllib.error.HTTPError as exc:
                data = exc.read()
                self.send_response(exc.code)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception as exc:  # backend down / connection refused
                msg = str(exc).encode()
                self.send_response(502)
                self.send_header("Content-Length", str(len(msg)))
                self.end_headers()
                self.wfile.write(msg)

        do_GET = _proxy
        do_POST = _proxy
        do_PUT = _proxy
        do_DELETE = _proxy

    return _Handler


class ReverseProxy:
    def __init__(self, apps_domain: str = "apps.winkapiserver.org", port: int = 0) -> None:
        self.apps_domain = apps_domain
        self.registry = RouteRegistry()
        self._server = ThreadingHTTPServer(("127.0.0.1", port), _make_handler(self.registry, apps_domain))
        self.port = self._server.server_address[1]
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def register(self, slug: str, host_port: int) -> str:
        self.registry.register(slug, host_port)
        return f"https://{slug}.{self.apps_domain}/"

    def unregister(self, slug: str) -> None:
        self.registry.unregister(slug)
