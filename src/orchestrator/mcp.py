"""Model Context Protocol (MCP) server manager for Jarvis.

Stores configured MCP server endpoints in <BASE_DIR>/config/mcp_servers.json
and provides helper methods to validate URLs, ping/test servers, and toggle them.
"""
import json
import logging
import os
import urllib.request
import urllib.error
from typing import Any, Dict, List, Tuple

from config import BASE_DIR

logger = logging.getLogger("jarvis")

_CONFIG_PATH = BASE_DIR / "config" / "mcp_servers.json"


def get_servers() -> List[Dict[str, Any]]:
    """Return all configured MCP servers."""
    if not _CONFIG_PATH.exists():
        return []
    try:
        data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
        return []
    except Exception as e:
        logger.warning("Failed to read mcp_servers.json: %s", e)
        return []


def _save_servers(servers: List[Dict[str, Any]]) -> None:
    """Safely save the servers list to disk."""
    try:
        _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(servers, indent=2), encoding="utf-8")
        os.replace(tmp, _CONFIG_PATH)
    except Exception as e:
        logger.error("Failed to save mcp_servers.json: %s", e)
        raise RuntimeError(f"Failed to save MCP server configuration: {e}")


def add_server(name: str, url: str, server_type: str = "http", description: str = "") -> Dict[str, Any]:
    """Add or update an MCP server."""
    name = (name or "").strip()
    url = (url or "").strip()
    if not name:
        raise ValueError("Server name is required.")
    if not url or not (url.startswith("http://") or url.startswith("https://") or url.startswith("mcp://") or url.startswith("sse://")):
        raise ValueError("Valid server URL is required (must start with http://, https://, mcp://, or sse://).")

    servers = get_servers()
    entry = {
        "name": name,
        "url": url,
        "type": server_type,
        "enabled": True,
        "description": description.strip()
    }

    for idx, s in enumerate(servers):
        if s.get("name") == name:
            servers[idx] = entry
            _save_servers(servers)
            return entry

    servers.append(entry)
    _save_servers(servers)
    return entry


def delete_server(name: str) -> bool:
    """Delete an MCP server by name."""
    servers = get_servers()
    initial_len = len(servers)
    servers = [s for s in servers if s.get("name") != name]
    if len(servers) < initial_len:
        _save_servers(servers)
        return True
    return False


def toggle_server(name: str, enabled: bool) -> Dict[str, Any]:
    """Enable or disable an MCP server."""
    servers = get_servers()
    for s in servers:
        if s.get("name") == name:
            s["enabled"] = bool(enabled)
            _save_servers(servers)
            return s
    raise KeyError(f"MCP server '{name}' not found.")


def test_server(url: str) -> Tuple[bool, str]:
    """Test connection to an MCP server URL."""
    url = (url or "").strip()
    if not url:
        return False, "Empty URL provided."

    # Normalize protocol for HTTP ping testing
    test_url = url
    if test_url.startswith("sse://"):
        test_url = "http://" + test_url[6:]
    elif test_url.startswith("mcp://"):
        test_url = "http://" + test_url[6:]

    try:
        req = urllib.request.Request(
            test_url,
            headers={"User-Agent": "Jarvis-MCP-Client/1.0", "Accept": "application/json, text/event-stream, */*"},
            method="GET"
        )
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            status = resp.getcode()
            if 200 <= status < 400:
                return True, f"Connected (HTTP {status})"
            return False, f"Server responded with status {status}"
    except urllib.error.HTTPError as e:
        # Many MCP servers return 405 Method Not Allowed on GET if they expect POST/SSE, or 400/401
        # A response from the HTTP server itself proves the endpoint exists and is reachable
        if e.code in (400, 401, 403, 404, 405, 406):
            return True, f"Server reachable (HTTP {e.code})"
        return False, f"HTTP error {e.code}: {e.reason}"
    except Exception as e:
        return False, f"Connection failed: {str(e)}"
