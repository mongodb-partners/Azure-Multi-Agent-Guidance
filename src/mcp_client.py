"""MongoDB MCP tool configuration for Foundry agents.

Wraps the reused MongoDB MCP Container App as a Foundry `McpTool` (same pattern as
`AzureFoundry_MongMCP_IntegrationPOC/agent_asst.py`). The MCP server's connection string
already targets FabricCluster; database/collection are passed per-query as tool args, so
no per-agent server config is needed.
"""
from __future__ import annotations

from .settings import get_settings

MCP_SERVER_LABEL = "mongodbMCP"

# Read-only MCP tool surface for the demo. When MONGODB_ALLOW_WRITE=true we also expose
# write tools (kept off by default for safety).
READ_TOOLS = ["find", "aggregate", "count", "list-collections", "collection-schema"]
WRITE_TOOLS = ["insert-many", "update-many", "delete-many"]


def allowed_mcp_tools() -> list[str]:
    s = get_settings()
    tools = list(READ_TOOLS)
    if s.allow_write:
        tools += WRITE_TOOLS
    return tools


def mcp_tool_dict() -> dict:
    """Hosted-MCP tool spec for the Responses API.

    This is the tool format the Azure OpenAI **Responses API** expects (the specialists run
    on Responses, not the classic Agents threads+runs API, because reasoning-class deployments
    such as gpt-5.x reject the `top_p` the Agents run loop injects). The service connects to
    the MongoDB MCP Container App directly and auto-approves calls (`require_approval="never"`).
    Database/collection are passed per-query as tool args, so no per-agent server config.
    """
    s = get_settings()
    s.require("mongodb_mcp_url")
    return {
        "type": "mcp",
        "server_label": MCP_SERVER_LABEL,
        "server_url": s.mongodb_mcp_url,
        "require_approval": "never",
        "allowed_tools": allowed_mcp_tools(),
    }
