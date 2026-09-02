"""Unit tests for the self-hosted MCP tool spec + write-gate + API-key header (no network)."""
from __future__ import annotations

import pytest

import src.mcp_client as mcp
from src.settings import Settings

pytestmark = pytest.mark.unit

_MCP_URL = "https://mcp-server.internal.azurecontainerapps.io/mcp"


def _patch_settings(monkeypatch, **overrides):
    base = dict(
        mongodb_mcp_url=_MCP_URL,
        mcp_api_key="",
        mcp_api_key_header="X-MCP-Api-Key",
        allow_write=False,
    )
    base.update(overrides)
    monkeypatch.setattr(mcp, "get_settings", lambda: Settings(**base))


def test_read_only_tools_by_default(monkeypatch):
    _patch_settings(monkeypatch, allow_write=False)
    tools = mcp.allowed_mcp_tools()
    assert set(tools) == set(mcp.READ_TOOLS)
    assert not any(t in tools for t in mcp.WRITE_TOOLS)


def test_write_gate_adds_write_tools(monkeypatch):
    _patch_settings(monkeypatch, allow_write=True)
    tools = mcp.allowed_mcp_tools()
    for t in mcp.WRITE_TOOLS:
        assert t in tools
    for t in mcp.READ_TOOLS:
        assert t in tools


def test_no_connect_tool_in_surface(monkeypatch):
    # The self-hosted MCP has a preconfigured connection — no `connect`/`remote-atlas-connect`.
    _patch_settings(monkeypatch, allow_write=True)
    tools = mcp.allowed_mcp_tools()
    assert not any("connect" in t for t in tools)


def test_mcp_tool_dict_shape(monkeypatch):
    _patch_settings(monkeypatch, allow_write=False)
    spec = mcp.mcp_tool_dict()
    assert spec["type"] == "mcp"
    assert spec["server_label"] == mcp.MCP_SERVER_LABEL
    assert spec["server_url"] == _MCP_URL
    assert spec["require_approval"] == "never"
    assert spec["allowed_tools"] == mcp.allowed_mcp_tools()


def test_no_header_when_no_api_key(monkeypatch):
    _patch_settings(monkeypatch, mcp_api_key="")
    spec = mcp.mcp_tool_dict()
    assert "headers" not in spec
    assert mcp.mcp_headers() == {}


def test_api_key_header_uses_non_authorization_name(monkeypatch):
    # Foundry rejects `Authorization` on registered MCP tools, so the header name must differ.
    _patch_settings(monkeypatch, mcp_api_key="s3cret", mcp_api_key_header="X-MCP-Api-Key")
    spec = mcp.mcp_tool_dict()
    assert spec["headers"] == {"X-MCP-Api-Key": "s3cret"}
    assert "Authorization" not in spec["headers"]


def test_mcp_tool_dict_requires_url(monkeypatch):
    _patch_settings(monkeypatch, mongodb_mcp_url="")
    with pytest.raises(RuntimeError):
        mcp.mcp_tool_dict()
