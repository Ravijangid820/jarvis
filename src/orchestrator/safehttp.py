"""Outbound HTTP for URLs the server was *told* to fetch (MCP endpoints, Home Assistant).

Two different risks live here, and only one of them is the textbook SSRF.

**The one that applies.** A remote endpoint we were pointed at answers `302 Location: …` and picks
where the next request goes. That target was never reviewed by anyone, urllib follows it without
asking, and — the part that actually costs something — urllib forwards the original `Authorization`
header to it, so a compromised or merely hostile MCP server can harvest the Home Assistant token by
bouncing us somewhere it controls. Redirects are capped here, re-checked against the same rules as
the original URL, and stripped of credentials the moment the host changes.

**The one that doesn't, quite.** "Block RFC1918/loopback so the URL can't point inward" is the
standard advice, and it is wrong for this project: Jarvis is a self-hosted, LAN-first box whose
whole purpose is talking to services on 192.168.x.x. Home Assistant *is* on the LAN. An MCP server
on 127.0.0.1 is a normal way to run one. Blocking those would break the product to defend against
an admin attacking their own machine — and the endpoints that accept these URLs are admin-only.

So the default posture is: private and loopback addresses are allowed, link-local is not
(169.254.0.0/16 is cloud-metadata territory and has no legitimate use here), and the genuinely
untrusted caller — a demo-mode visitor, who is an admin of their own household — is kept away from
these endpoints entirely at the route level. Deployments that are actually exposed can tighten to
the textbook rule with JARVIS_HTTP_ALLOW_LOCAL=0.

Known limit, stated rather than papered over: the guard resolves the hostname, then urllib resolves
it again when it connects. A DNS entry that changes between those two moments (rebinding) defeats
the address check. Closing that means pinning the resolved IP into the connection, which urllib
cannot do without a custom transport; against an admin-only surface it is not worth that machinery.
"""
import ipaddress
import os
import socket
import urllib.error
import urllib.request
from typing import Optional
from urllib.parse import urlsplit

__all__ = ["BlockedURL", "guard_url", "urlopen", "allow_local_default"]

# Bound low deliberately. Legitimate MCP endpoints and Home Assistant redirect once (http→https,
# or a trailing-slash normalisation) or not at all; a chain longer than this is someone playing.
MAX_REDIRECTS = 3


class BlockedURL(ValueError):
    """The URL is refused before any connection is made. Subclasses ValueError so existing
    callers, which already turn ValueError into a 400 with the message, need no changes."""


def allow_local_default() -> bool:
    """Are loopback/private targets permitted? Read per call, so tests and operators can flip it."""
    return os.environ.get("JARVIS_HTTP_ALLOW_LOCAL", "1") != "0"


def _check_address(host: str, allow_local: bool) -> None:
    """Resolve `host` and refuse if ANY address it answers with is out of bounds.

    Any, not all: a name resolving to one public and one link-local address is a rebinding attempt
    wearing a hat, and there is no benign reason for it.
    """
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise BlockedURL(f"Could not resolve host '{host}': {e}") from None

    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        # ::ffff:169.254.169.254 must be judged as the IPv4 address it is.
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped

        if ip.is_link_local:
            raise BlockedURL(
                f"'{host}' resolves to the link-local address {ip} — that range carries cloud "
                "instance-metadata services and is never a real MCP or Home Assistant endpoint.")
        if ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            raise BlockedURL(f"'{host}' resolves to {ip}, which is not a routable endpoint.")
        if not allow_local and (ip.is_loopback or ip.is_private):
            raise BlockedURL(
                f"'{host}' resolves to the internal address {ip}, and JARVIS_HTTP_ALLOW_LOCAL=0 "
                "restricts this server to public endpoints.")


def guard_url(url: str, allow_local: Optional[bool] = None) -> str:
    """Validate a URL the server has been asked to fetch. Returns it stripped; raises BlockedURL."""
    url = (url or "").strip()
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise BlockedURL("Only complete http:// or https:// URLs are supported.")
    if parsed.username or parsed.password:
        raise BlockedURL("Embedded credentials in the URL are not supported.")
    if not parsed.hostname:
        raise BlockedURL("The URL has no host.")
    _check_address(parsed.hostname,
                   allow_local_default() if allow_local is None else allow_local)
    return url


class _GuardedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Applies guard_url to every hop, and drops credentials when the host changes.

    urllib's own handler copies the request headers onto the redirected request verbatim, which
    means `Authorization: Bearer <home-assistant-token>` follows a 302 to any host on the internet.
    """

    max_redirections = MAX_REDIRECTS
    max_repeats = MAX_REDIRECTS

    def __init__(self, allow_local: bool):
        self.allow_local = allow_local

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        guard_url(newurl, allow_local=self.allow_local)
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urlsplit(newurl).netloc.lower() != urlsplit(req.full_url).netloc.lower():
            for header in ("Authorization", "Cookie", "Mcp-Session-Id"):
                new.headers.pop(header, None)
                new.headers.pop(header.capitalize(), None)
                # urllib title-cases header keys on the way in (Authorization → Authorization,
                # but mcp-session-id → Mcp-session-id), so match case-insensitively too.
                for key in [k for k in new.headers if k.lower() == header.lower()]:
                    del new.headers[key]
        return new


def urlopen(req, timeout: float, allow_local: Optional[bool] = None):
    """urllib.request.urlopen with the guard applied to the target and to every redirect.

    Accepts a Request or a URL string, exactly like the function it replaces.
    """
    if allow_local is None:
        allow_local = allow_local_default()
    url = req.full_url if isinstance(req, urllib.request.Request) else req
    guard_url(url, allow_local=allow_local)
    # A fresh opener per call: these are rare, and a module-level one would cache the env flag and
    # silently ignore an operator flipping JARVIS_HTTP_ALLOW_LOCAL.
    opener = urllib.request.build_opener(_GuardedRedirectHandler(allow_local))
    return opener.open(req, timeout=timeout)
