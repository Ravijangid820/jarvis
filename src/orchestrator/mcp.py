"""Model Context Protocol (MCP) server manager for Jarvis.

Stores configured MCP server endpoints in <BASE_DIR>/config/mcp_servers.json
and provides helper methods to validate URLs, ping/test servers, and toggle them.
"""
import json
import logging
import os
import urllib.request
from typing import Any, Dict, List, Tuple

import safehttp
from config import APP_VERSION, BASE_DIR

logger = logging.getLogger("jarvis")

_CONFIG_PATH = BASE_DIR / "config" / "mcp_servers.json"
_PROTOCOL_VERSION = "2025-03-26"
# Derived from pyproject, never hardcoded: a stale literal here would be a third version
# source disagreeing with the git tag and the image tag.
_CLIENT_INFO = {"name": "jarvis", "version": APP_VERSION}
_MAX_DISCOVERED_TOOLS = 32
# Bound the read: an MCP endpoint is a remote party we don't control, and this box has 8 GB.
_MAX_RESPONSE_BYTES = 1024 * 1024


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
    # Checked at SAVE time as well as at fetch time, so a bad endpoint is rejected in the admin
    # form rather than accepted and then failing every time a tool is discovered.
    url = safehttp.guard_url(url)

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
    """Verify the MCP lifecycle and report actual tool discovery, not a mere HTTP ping."""
    try:
        tools = discover_tools(url)
        return True, f"Connected — discovered {len(tools)} tool{'s' if len(tools) != 1 else ''}."
    except ValueError as e:
        return False, str(e)
    except Exception as e:
        logger.info("MCP test failed for %s: %s", url, e)
        return False, f"MCP handshake failed: {str(e)[:160]}"


def _decode_rpc_response(raw: bytes, content_type: str) -> Dict[str, Any]:
    text = raw.decode("utf-8", errors="replace").strip()
    if "text/event-stream" in content_type:
        for line in text.splitlines():
            if line.startswith("data:"):
                text = line[5:].strip()
                break
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("MCP endpoint returned an invalid JSON-RPC response.")
    if "error" in data:
        raise ValueError(f"MCP error: {data['error'].get('message', 'unknown error')}")
    return data


def _rpc(url: str, payload: Dict[str, Any], session_id: str | None = None) -> Tuple[Dict[str, Any], str | None]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": _PROTOCOL_VERSION,
        "User-Agent": "Jarvis-MCP-Client/3.0",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    # safehttp, not urllib: caps the redirect chain, re-checks each hop, and drops the session id
    # if the endpoint bounces us to a different host. See safehttp.py for what it does NOT defend.
    with safehttp.urlopen(req, timeout=8.0) as response:
        raw = response.read(_MAX_RESPONSE_BYTES + 1)     # don't trust the server's advertised size
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise ValueError("MCP endpoint returned an oversized response.")
        data = _decode_rpc_response(raw, response.headers.get("Content-Type", ""))
        return data, response.headers.get("Mcp-Session-Id") or session_id


def discover_tools(url: str) -> List[Dict[str, Any]]:
    """Perform a Streamable HTTP MCP handshake and return validated tool definitions.

    Discovery is intentionally request-scoped. It avoids long-lived server sessions and
    any cross-user state until a tool-execution policy is in place.
    """
    url = safehttp.guard_url(url)
    initialized, session_id = _rpc(url, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": _PROTOCOL_VERSION, "capabilities": {}, "clientInfo": _CLIENT_INFO},
    })
    result = initialized.get("result") or {}
    if not isinstance(result.get("capabilities"), dict) or "tools" not in result["capabilities"]:
        raise ValueError("MCP server does not advertise tool support.")
    _rpc(url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, session_id)
    listed, _ = _rpc(url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, session_id)
    tools = (listed.get("result") or {}).get("tools")
    if not isinstance(tools, list):
        raise ValueError("MCP server returned an invalid tools/list response.")
    valid = []
    for tool in tools[:_MAX_DISCOVERED_TOOLS]:
        if not isinstance(tool, dict) or not isinstance(tool.get("name"), str) or not tool["name"]:
            continue
        schema = tool.get("inputSchema") or {"type": "object", "properties": {}}
        if not isinstance(schema, dict) or schema.get("type", "object") != "object":
            continue
        valid.append({"name": tool["name"], "description": str(tool.get("description") or "")[:500],
                      "inputSchema": schema})
    return valid
