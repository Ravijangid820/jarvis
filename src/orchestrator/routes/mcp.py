"""MCP server registry — discovery and review only.

Nothing here executes an MCP tool, and nothing wires one into the model's tool menu. That waits on
a per-tool allowlist and an answer to whose authority a remote tool would run under.

`import mcp` below is the top-level client module (src/orchestrator/mcp.py), not this file: Python
3 has no implicit relative imports, so the names do not collide.
"""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

import deps
import mcp
from config import logger

router = APIRouter(tags=["mcp"])


class MCPServerRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    url: str = Field(..., min_length=5, max_length=500)
    type: str = Field(default="http", max_length=32)
    description: str = Field(default="", max_length=200)


class MCPServerTestRequest(BaseModel):
    url: str = Field(..., min_length=5, max_length=500)


# The MCP server list is process-wide, not per-household, and configuring one makes this server
# fetch a URL a caller chose. In demo mode every visitor is an admin OF THEIR OWN HOUSEHOLD, so
# deps.require_admin does not mean "trusted operator" there — it means "anyone who clicked Try it".
# Until MCP config is household-scoped, demo households stay out of it entirely; that, rather
# than an address blocklist, is what keeps untrusted callers away from this fetch (safehttp.py).
DEMO_DETAIL = "MCP servers are configured by the operator; they are read-only in public Demo Mode."


@router.get("/mcp/servers")
def get_mcp_servers(request: Request):
    """Return configured MCP tool servers."""
    deps.require_admin(request)
    return {"servers": mcp.get_servers()}


@router.post("/mcp/servers")
def add_mcp_server(req: MCPServerRequest, request: Request):
    """Add or update an MCP server."""
    deps.require_admin(request)
    deps.require_not_demo(DEMO_DETAIL)
    try:
        server = mcp.add_server(req.name, req.url, req.type, req.description)
        return {"status": "ok", "server": server}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to add server: {e}")


@router.delete("/mcp/servers/{name}")
def delete_mcp_server(name: str, request: Request):
    """Delete an MCP server by name."""
    deps.require_admin(request)
    deps.require_not_demo(DEMO_DETAIL)
    if mcp.delete_server(name):
        return {"status": "ok"}
    raise HTTPException(status_code=404, detail="Server not found")


@router.post("/mcp/test")
def test_mcp_server(req: MCPServerTestRequest, request: Request):
    """Test connection to an MCP server URL."""
    deps.require_admin(request)
    deps.require_not_demo(DEMO_DETAIL)
    ok, detail = mcp.test_server(req.url)
    return {"ok": ok, "detail": detail}


@router.get("/mcp/servers/{name}/tools")
def get_mcp_server_tools(name: str, request: Request):
    """Discover a configured server's MCP tools for review before any tool is enabled."""
    deps.require_admin(request)
    server = next((item for item in mcp.get_servers() if item.get("name") == name), None)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    try:
        return {"server": name, "tools": mcp.discover_tools(server.get("url", ""))}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.info("MCP tool discovery failed for %s: %s", name, e)
        raise HTTPException(status_code=502, detail="MCP tool discovery failed")
