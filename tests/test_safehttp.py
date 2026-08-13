"""Outbound-fetch guard (safehttp.py).

The redirect tests use REAL local HTTP servers rather than a monkeypatched handler. The claim
being made is "a hostile endpoint cannot walk off with the Home Assistant token", and the thing
that would leak it is urllib's own redirect machinery — mocking that away would test the mock.
Two servers on 127.0.0.1 and 127.0.0.2 give a genuine cross-host redirect over loopback.
"""
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "orchestrator"))

import safehttp  # noqa: E402


# --------------------------------------------------------------------------- guard_url

@pytest.mark.parametrize("url", [
    "ftp://example.com/x",
    "file:///etc/passwd",
    "notaurl",
    "http://",
    "",
])
def test_non_http_urls_are_refused(url):
    with pytest.raises(safehttp.BlockedURL):
        safehttp.guard_url(url)


def test_embedded_credentials_are_refused():
    with pytest.raises(safehttp.BlockedURL, match="credentials"):
        safehttp.guard_url("http://user:pass@example.com/mcp")


@pytest.mark.parametrize("host", [
    "169.254.169.254",          # AWS/GCP/Azure instance metadata
    "169.254.1.1",
    "[::ffff:169.254.169.254]",  # the same address wearing an IPv6 hat
])
def test_link_local_is_always_refused(host):
    """No legitimate MCP or HA endpoint lives here, so this one is blocked even in the
    default LAN-permissive posture."""
    with pytest.raises(safehttp.BlockedURL, match="link-local"):
        safehttp.guard_url(f"http://{host}/latest/meta-data/")


def test_lan_and_loopback_are_allowed_by_default():
    """The deliberate deviation from the textbook SSRF fix: Home Assistant is ON the LAN and a
    local MCP server is a normal way to run one. Blocking these would break the product."""
    assert safehttp.guard_url("http://192.168.0.101:8123/api/")
    assert safehttp.guard_url("http://127.0.0.1:3000/mcp")
    assert safehttp.guard_url("http://10.0.0.5/mcp")


@pytest.mark.parametrize("url", [
    "http://192.168.0.101:8123/api/",
    "http://127.0.0.1:3000/mcp",
    "http://10.0.0.5/mcp",
    "http://172.16.4.4/mcp",
])
def test_strict_mode_refuses_internal_targets(url, monkeypatch):
    """JARVIS_HTTP_ALLOW_LOCAL=0 is for deployments that really are exposed."""
    monkeypatch.setenv("JARVIS_HTTP_ALLOW_LOCAL", "0")
    with pytest.raises(safehttp.BlockedURL, match="internal address"):
        safehttp.guard_url(url)


def test_strict_mode_is_read_per_call(monkeypatch):
    """The flag must not be captured at import time, or flipping it needs a restart to matter."""
    monkeypatch.setenv("JARVIS_HTTP_ALLOW_LOCAL", "1")
    assert safehttp.guard_url("http://127.0.0.1:9/x")
    monkeypatch.setenv("JARVIS_HTTP_ALLOW_LOCAL", "0")
    with pytest.raises(safehttp.BlockedURL):
        safehttp.guard_url("http://127.0.0.1:9/x")


def test_unresolvable_host_is_a_clean_error():
    """ENVIRONMENT-DEPENDENT: needs .invalid to actually NXDOMAIN.

    RFC 6761 reserves the TLD precisely so this is safe, but a resolver configured with a wildcard
    upstream — some captive portals and ISP "search assistance" services do this — will answer with
    an address instead, and then guard_url has something to resolve and no reason to complain. A
    failure here is far more likely to be the network than the code.
    """
    with pytest.raises(safehttp.BlockedURL, match="resolve"):
        safehttp.guard_url("http://no-such-host.invalid/mcp")


# --------------------------------------------------------------------------- redirect behaviour

class _Recorder(BaseHTTPRequestHandler):
    """Serves whatever its server was told to serve and records the headers it was sent."""

    def do_GET(self):                                    # noqa: N802 (BaseHTTPRequestHandler API)
        self.server.seen.append({k.lower(): v for k, v in self.headers.items()})
        target = self.server.redirect_to
        if target and self.path != "/final":
            self.send_response(302)
            self.send_header("Location", target)
            self.end_headers()
            return
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):                           # keep pytest output clean
        pass


def _serve(host, redirect_to=None):
    srv = ThreadingHTTPServer((host, 0), _Recorder)
    srv.seen = []
    srv.redirect_to = redirect_to
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
def two_servers():
    """A 'victim' (holds the token) and an 'attacker' on a different host, both on loopback.

    ENVIRONMENT-DEPENDENT: binding 127.0.0.2 works on Linux, where the whole 127.0.0.0/8 block is
    attached to the loopback interface, and this project's CI is Linux. On macOS and the BSDs only
    127.0.0.1 is configured by default, so this fixture fails to bind there until someone runs
    `sudo ifconfig lo0 alias 127.0.0.2 up`. Two distinct hosts are the point of the test — a
    redirect within the SAME host is allowed to keep its Authorization header — so there is no
    single-address version of it.
    """
    attacker = _serve("127.0.0.2")
    victim = _serve("127.0.0.1", redirect_to=f"http://127.0.0.2:{attacker.server_port}/final")
    yield victim, attacker
    victim.shutdown()
    attacker.shutdown()


def test_authorization_is_not_forwarded_across_a_cross_host_redirect(two_servers):
    """The finding that actually costs something: urllib copies Authorization onto the redirected
    request, so an endpoint that answers 302 can harvest the Home Assistant long-lived token."""
    victim, attacker = two_servers
    req = urllib.request.Request(
        f"http://127.0.0.1:{victim.server_port}/api/",
        headers={"Authorization": "Bearer ha-long-lived-token", "Mcp-Session-Id": "sess-1"},
    )
    with safehttp.urlopen(req, timeout=5) as r:
        assert r.status == 200

    assert victim.seen and victim.seen[0]["authorization"] == "Bearer ha-long-lived-token"
    assert attacker.seen, "the redirect was not followed — test would prove nothing"
    assert "authorization" not in attacker.seen[0]
    assert "mcp-session-id" not in attacker.seen[0]


def test_authorization_survives_a_same_host_redirect():
    """Dropping it unconditionally would break ordinary trailing-slash redirects on HA itself."""
    srv = _serve("127.0.0.1")
    srv.redirect_to = f"http://127.0.0.1:{srv.server_port}/final"
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{srv.server_port}/api",
                                     headers={"Authorization": "Bearer tok"})
        with safehttp.urlopen(req, timeout=5) as r:
            assert r.status == 200
        assert srv.seen[-1]["authorization"] == "Bearer tok"
    finally:
        srv.shutdown()


def test_redirect_to_a_blocked_address_is_refused():
    """The reason a blocklist is worth having at all: the second hop is chosen by the endpoint,
    not by the admin who typed the URL."""
    srv = _serve("127.0.0.1", redirect_to="http://169.254.169.254/latest/meta-data/")
    try:
        with pytest.raises(safehttp.BlockedURL, match="link-local"):
            safehttp.urlopen(f"http://127.0.0.1:{srv.server_port}/mcp", timeout=5)
    finally:
        srv.shutdown()


def test_ha_never_calls_urllib_urlopen_directly():
    """Three call sites in ha.py carry the long-lived token, and converting only one of them
    (which is what happened while writing this) leaves the leak open in the other two. The LLM
    client is deliberately exempt: it talks to 127.0.0.1 with no credentials, and routing it
    through the guard would break it under JARVIS_HTTP_ALLOW_LOCAL=0.
    """
    src = (Path(__file__).resolve().parents[1] / "src" / "orchestrator" / "ha.py").read_text()
    offenders = [ln.strip() for ln in src.splitlines()
                 if "urllib.request.urlopen" in ln and not ln.strip().startswith("#")]
    assert not offenders, f"use safehttp.urlopen — the token must not follow a redirect: {offenders}"


def test_redirect_chains_are_capped():
    srv = _serve("127.0.0.1")
    srv.redirect_to = None
    # Point it at itself on a path that is never "/final", so it loops forever if uncapped.
    srv.redirect_to = f"http://127.0.0.1:{srv.server_port}/loop"
    try:
        with pytest.raises(urllib.error.HTTPError):
            safehttp.urlopen(f"http://127.0.0.1:{srv.server_port}/loop", timeout=5)
        assert len(srv.seen) <= safehttp.MAX_REDIRECTS + 1
    finally:
        srv.shutdown()
