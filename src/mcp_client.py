"""Self-hosted MongoDB MCP tool configuration for Foundry agents.

The agents connect to a **self-hosted `mongodb/mongodb-mcp-server`** running in Azure Container
Apps (Streamable HTTP at `<fqdn>/mcp`). The MCP container is started with the MongoDB connection
string baked in (`MDB_MCP_CONNECTION_STRING`), so it holds a single preconfigured connection to the
target cluster — the agents do NOT authenticate to MongoDB and there is **no OAuth token to mint or
refresh**, and no `connect`/`remote-atlas-connect` handshake before queries.

If the MCP endpoint is exposed with external ingress it validates a shared **API-key header** (a
non-`Authorization` header, because the Foundry runtime rejects `Authorization` on registered MCP
tools). The header name/value come from settings (`MCP_API_KEY_HEADER` / `MCP_API_KEY`); when the
key is empty (internal-ingress or local dev) no header is sent.

Nothing here is hardcoded — the endpoint URL and API key come from settings/.env.
"""
from __future__ import annotations

from .settings import get_settings

MCP_SERVER_LABEL = "mongodbSelfHostedMCP"

# Read-only MCP tool surface for the demo. When MONGODB_ALLOW_WRITE=true we also expose write tools
# (kept off by default for safety). These names match the `mongodb/mongodb-mcp-server` tool set.
# No `connect` tool is needed: the MCP server has a preconfigured connection (MDB_MCP_CONNECTION_STRING).
READ_TOOLS = ["find", "aggregate", "count", "list-collections", "collection-schema", "list-databases"]
WRITE_TOOLS = ["insert-many", "update-many", "delete-many"]


def allowed_mcp_tools() -> list[str]:
    s = get_settings()
    tools = list(READ_TOOLS)
    if s.allow_write:
        tools += WRITE_TOOLS
    return tools


def mcp_headers() -> dict[str, str]:
    """Return the API-key header for the self-hosted MCP, or {} when no key is configured.

    Uses a NON-`Authorization` header name so the tool can also be registered on Foundry prompt/
    hosted agents (Foundry rejects an `Authorization` header on stored MCP tools). The value is a
    static, long-lived key — there is no token to refresh.
    """
    s = get_settings()
    if not s.mcp_api_key:
        return {}
    return {s.mcp_api_key_header: s.mcp_api_key}


def mcp_tool_dict() -> dict:
    """Hosted-MCP tool spec for the Responses API, pointed at the self-hosted ACA MCP server.

    `require_approval="never"` auto-approves the read (and optional write) tool calls. The MongoDB
    database/collection are passed per-query as tool args. If an API key is configured it is attached
    as a static header (see `mcp_headers`).
    """
    s = get_settings()
    s.require("mongodb_mcp_url")
    spec: dict = {
        "type": "mcp",
        "server_label": MCP_SERVER_LABEL,
        "server_url": s.mongodb_mcp_url,
        "require_approval": "never",
        "allowed_tools": allowed_mcp_tools(),
    }
    headers = mcp_headers()
    if headers:
        spec["headers"] = headers
    return spec
