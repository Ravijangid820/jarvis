"""HTTPS verification for the camera clients.

When `server.url` is https and `server.ca_cert` points at the local CA (see src/scripts/setup_tls.sh
on the server, then copy tls/ca.crt to the device), clients verify the server against that CA. For
plain http it's a no-op. Verification is never disabled — if you use https without a ca_cert, the
default system CAs apply (which won't trust a local CA, so it fails closed rather than silently
skipping the check).
"""
import ssl
from pathlib import Path

from .paths import base_dir


def _ca_path(cfg):
    ca = (cfg.get("server", {}) or {}).get("ca_cert")
    if not ca:
        return None
    p = Path(ca)
    if not p.is_absolute():
        p = base_dir() / p
    return str(p) if p.exists() else None


def ssl_context(cfg):
    """An ssl.SSLContext for urllib that verifies against the configured CA, or None for defaults."""
    ca = _ca_path(cfg)
    return ssl.create_default_context(cafile=ca) if ca else None


def verify_arg(cfg):
    """Value for requests' `verify=`: the CA path if configured, else True (default system trust)."""
    return _ca_path(cfg) or True
