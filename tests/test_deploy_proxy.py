"""Reverse proxy routing (step 4a) — no Docker needed.

Honest: real backend HTTP servers, real requests through the proxy, routed by the
Host header. The proxy is the thing under test; the backends are genuine servers.
"""

from __future__ import annotations

import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from aios_core.deploy.proxy import ReverseProxy, slug_from_host

APPS_DOMAIN = "apps.winkapiserver.org"


def _backend(body: bytes):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def _get(port: int, host: str) -> tuple[int, str]:
    req = urllib.request.Request(f"http://127.0.0.1:{port}/", headers={"Host": host})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_slug_from_host():
    assert slug_from_host("one.apps.winkapiserver.org", APPS_DOMAIN) == "one"
    assert slug_from_host("two.apps.winkapiserver.org:443", APPS_DOMAIN) == "two"
    assert slug_from_host("apps.winkapiserver.org", APPS_DOMAIN) is None       # no slug label
    assert slug_from_host("a.b.apps.winkapiserver.org", APPS_DOMAIN) is None   # too many labels
    assert slug_from_host("evil.com", APPS_DOMAIN) is None


def test_proxy_routes_by_host():
    b1, p1 = _backend(b"APP-ONE")
    b2, p2 = _backend(b"APP-TWO")
    proxy = ReverseProxy(apps_domain=APPS_DOMAIN)
    proxy.start()
    try:
        url1 = proxy.register("one", p1)
        proxy.register("two", p2)
        assert url1 == "https://one.apps.winkapiserver.org/"

        assert _get(proxy.port, "one.apps.winkapiserver.org") == (200, "APP-ONE")
        assert _get(proxy.port, "two.apps.winkapiserver.org") == (200, "APP-TWO")

        # unknown slug -> 404
        assert _get(proxy.port, "nope.apps.winkapiserver.org")[0] == 404
        # unregister removes the route
        proxy.unregister("one")
        assert _get(proxy.port, "one.apps.winkapiserver.org")[0] == 404
    finally:
        proxy.shutdown()
        b1.shutdown()
        b2.shutdown()


def test_proxy_502_when_backend_down():
    proxy = ReverseProxy(apps_domain=APPS_DOMAIN)
    proxy.start()
    try:
        # register a route to a port with nothing listening
        proxy.register("dead", 59999)
        code, _ = _get(proxy.port, "dead.apps.winkapiserver.org")
        assert code == 502
    finally:
        proxy.shutdown()
