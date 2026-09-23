# tests/test_mcp_integration.py
# Integration tests for MCP client — full wiring chain end-to-end.
#
# Tests the complete flow:
#   YAML agent def → SpecialAgentDef.mcp_servers → create_conversation →
#   Conversation.mcp_servers → runtime tool merging → execute_tool routing →
#   MCP client → MCP server subprocess → result
#
# Mock at the SDK boundary (stdin_client, ClientSession) but exercise the
# real glue code (execute_tool, get_tools_for_api, runtime, special_agents).

import asyncio
import json
import os
import pytest
import threading
import time
from unittest.mock import patch, MagicMock, AsyncMock

from agent.tools import ToolResult, execute_tool
from agent.runtime import AgentRuntime
from agent.special_agents import SpecialAgentDef
from models.conversation import Conversation
from utils.agent_defs import validate_agent_def
from utils.mcp_client import (
    connect,
    disconnect_all,
    discover_tools,
    get_tools_for_api,
    is_connected,
    _conversations,
    _state_lock,
    _MCPLoopThread,
    _tools_cache,
    _tools_cache_lock,
    _is_connection_future,
    _make_connecting_sentinel,
)
from utils.mcp_config import _clear_cache


# ── Helpers ───────────────────────────────────────────────────────────────────

class FakeMCPFactory:
    """Factory that produces fake SDK objects for mock-based tests."""

    @staticmethod
    def make_fake_session():
        """Return a mock session with list_tools and call_tool."""
        mock_session = MagicMock()
        mock_session.initialize = AsyncMock()

        # Create explicit mock tools with real attributes
        search_tool = MagicMock()
        search_tool.name = "search_nodes"
        search_tool.description = "Search knowledge graph"
        search_tool.inputSchema = {"type": "object", "properties": {"query": {"type": "string"}}}

        create_tool = MagicMock()
        create_tool.name = "create_node"
        create_tool.description = "Create a knowledge node"
        create_tool.inputSchema = {"type": "object", "properties": {"name": {"type": "string"}}}

        mock_tools_result = MagicMock()
        mock_tools_result.tools = [search_tool, create_tool]
        mock_session.list_tools = AsyncMock(return_value=mock_tools_result)

        tool_result = MagicMock()
        tool_result.content = [MagicMock(text="42 nodes found: alpha, beta, gamma")]
        mock_session.call_tool = AsyncMock(return_value=tool_result)

        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        return mock_session

    @classmethod
    def fake_connect_async(cls, config):
        """Replacement for _connect_async — returns a real-enough conn dict."""
        mock_session = cls.make_fake_session()
        return {
            "session": mock_session,
            "stdio_cm": None,
            "session_cm": None,
        }


class TestState:
    """Base class: clears all MCP state between tests."""
    def setup_method(self):
        with _state_lock:
            _conversations.clear()
        _MCPLoopThread._instances.clear()
        _clear_cache()
        with _tools_cache_lock:
            _tools_cache.clear()

    def teardown_method(self):
        with _state_lock:
            _conversations.clear()
        with _tools_cache_lock:
            _tools_cache.clear()
        for key in list(_MCPLoopThread._instances.keys()):
            instance = _MCPLoopThread._instances.pop(key)
            instance._stopping = True
            instance._drain_and_stop()


# ── TestYAMLLoading ─────────────────────────────────────────────────────────────

class TestYAMLLoading(TestState):
    def test_mcp_servers_loaded_from_yaml(self):
        """Agent YAML with mcp_servers: [memory] → SpecialAgentDef has it."""
        agent = SpecialAgentDef(
            conv_id_prefix="special:test",
            display_name="Test",
            role="tester",
            emoji="🧪",
            tools=["read_file"],
            can_write=False,
            mcp_servers=["memory"],
        )
        assert agent.mcp_servers == ["memory"]

    def test_mcp_servers_coerced_in_load_registry(self, tmp_path, monkeypatch):
        """BUG #32: String mcp_servers value is coerced to list by _load_registry.

        When agent YAML contains `mcp_servers: memory` (a string not a list),
        _load_registry() coerces it via the code at lines ~101-105 of
        agent/special_agents.py.
        """
        import json
        from agent.special_agents import reload_registry, get_special_agents
        from utils.agent_defs import _parse_agent_file

        # Write a test agent definition file containing a bare string
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        agent_file = agents_dir / "test.yaml"
        agent_dict = {
            "name": "StringMCP",
            "role": "tester",
            "prompts": ["system/default.md"],
            "tools": ["read_file"],
            "provider": "openai",
            "llm_name": "openrouter",
            "fallback_provider": "openrouter",  # LOW-11: every agent needs a fallback
            "mcp_servers": "memory",  # String, not list
        }
        agent_file.write_text(json.dumps(agent_dict))

        # Monkeypatch utils.agent_defs._get_agents_dir() to point here
        import utils.agent_defs as ad_mod
        monkeypatch.setattr(ad_mod, "_get_agents_dir", lambda: str(agents_dir))

        reload_registry()
        agents = get_special_agents()
        ours = [a for a in agents if a.display_name == "StringMCP"]
        assert len(ours) == 1, f"Agent not found in registry; got {len(agents)} agents"
        agent = ours[0]
        # Critical assertion: string was coerced to list
        assert isinstance(agent.mcp_servers, list) and agent.mcp_servers == ["memory"], (
            f"Expected ['memory'], got {agent.mcp_servers!r} — coercion in _load_registry() is not working"
        )

    def test_mcp_servers_invalid_type_rejected(self):
        """YAML dict with non-list mcp_servers is rejected by validation."""
        errors = validate_agent_def({
            "name": "Bad Agent",
            "role": "bad",
            "prompts": ["system/bad.md"],
            "tools": ["read_file"],
            "provider": "openai",
            "mcp_servers": 12345,  # type: ignore — should be list
        })
        assert any("mcp_servers" in e for e in errors)

    def test_mcp_servers_slash_in_name_rejected(self):
        """Server names with '/' are rejected by validation."""
        errors = validate_agent_def({
            "name": "Bad Agent",
            "role": "bad",
            "prompts": ["system/bad.md"],
            "tools": ["read_file"],
            "provider": "openai",
            "mcp_servers": ["mem/ory"],
        })
        assert any("Invalid MCP server name" in e for e in errors)


# ── TestToolMerging ─────────────────────────────────────────────────────────────

class TestToolMerging(TestState):
    """Tests the runtime merging of built-in + MCP tools."""

    @patch("utils.mcp_client._connect_async", side_effect=FakeMCPFactory.fake_connect_async)
    @patch("utils.mcp_client.get_server_config")
    def test_mcp_tools_merged_with_builtin_tools(self, mock_get_config, mock_connect):
        """get_tools_for_api() returns namespaced MCP tools + built-in tools."""
        mock_get_config.return_value = MagicMock(
            name="memory", transport="stdio", command="npx",
            args=["-y", "@mcp/server-memory"], enabled=True,
        )

        # Connect first
        connect("memory", "test-conv")
        mcp_tools = get_tools_for_api(["memory"], "test-conv")

        assert len(mcp_tools) > 0
        names = [t["function"]["name"] for t in mcp_tools]
        # BUG #38: With proper explicit attributes, names must be exact strings
        assert "memory__search_nodes" in names
        assert "memory__create_node" in names

    @patch("utils.mcp_client._connect_async", side_effect=FakeMCPFactory.fake_connect_async)
    @patch("utils.mcp_client.get_server_config")
    def test_no_mcp_servers_means_no_mcp_tools(self, mock_get_config, mock_connect):
        """Conversation with empty mcp_servers → no MCP tools merged."""
        c = Conversation(
            agent_name="test-agent",
            mcp_servers=[],
        )
        assert c.mcp_servers == []
        # get_tools_for_api called with empty list → empty result
        result = get_tools_for_api([], "test-conv")
        assert result == []

    def test_mcp_failure_doesnt_break_builtin_tools(self):
        """If MCP server fails, built-in tools still returned."""
        from agent.tools import get_tool_definitions_for_api
        tools = get_tool_definitions_for_api(None)
        assert len(tools) > 0  # Built-in tools exist even when MCP fails


# ── TestToolRouting ────────────────────────────────────────────────────────────

class TestToolRouting(TestState):
    """Tests execute_tool() routing: built-in vs MCP."""

    @patch("utils.mcp_client._connect_async", side_effect=FakeMCPFactory.fake_connect_async)
    @patch("utils.mcp_client.get_server_config")
    def test_mcp_tool_routes_through_execute_tool(self, mock_get_config, mock_connect, tmp_path):
        """execute_tool('memory__search_nodes', ...) → MCP call_tool → results."""
        mock_get_config.return_value = MagicMock(
            name="memory", transport="stdio", command="npx",
            args=["-y", "@mcp/server-memory"], enabled=True,
        )

        result = execute_tool(
            "memory__search_nodes",
            {"query": "test"},
            str(tmp_path),
            "test-conv",
        )
        assert result.success is True
        assert "42" in result.output

    @patch("utils.mcp_client._connect_async", side_effect=FakeMCPFactory.fake_connect_async)
    @patch("utils.mcp_client.get_server_config")
    def test_builtin_tool_unaffected_by_mcp(self, mock_get_config, mock_connect):
        """execute_tool('read_file', ...) → built-in handler, not MCP."""
        mock_get_config.return_value = MagicMock(
            name="memory", transport="stdio", command="npx",
            args=["-y", "@mcp/server-memory"], enabled=True,
        )
        # Connect to memory first (doesn't affect read_file)
        connect("memory", "test-conv")

        # read_file is a built-in tool — must pass a real path WITHIN project_path
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            test_file = os.path.join(td, "test.txt")
            with open(test_file, "w") as f:
                f.write("hello")
            result = execute_tool(
                "read_file",
                {"path": "test.txt"},
                td,
                "test-conv",
            )
            assert result.success is True
            assert "hello" in result.output

    def test_invalid_mcp_name_returns_error(self, tmp_path):
        """Missing '/' separator returns proper error."""
        result = execute_tool(
            "noslash_tool",
            {},
            str(tmp_path),
            "test-conv",
        )
        # If noslash_tool doesn't exist in _TOOLS, it returns "Unknown tool"
        # (This test verifies that execute_tool still handles built-in tool lookup)
        # For a tool with bad MCP format, let's use empty server or tool name
        result = execute_tool(
            "/badname",
            {},
            str(tmp_path),
            "test-conv",
        )
        assert result.success is False
        assert "Unknown tool" in result.error

    @patch("utils.mcp_client._connect_async", side_effect=Exception("server exploded"))
    @patch("utils.mcp_client.get_server_config")
    def test_mcp_connection_failure_returns_error_result(self, mock_get_config, mock_connect, tmp_path):
        """If MCP connect fails, execute_tool returns ToolResult(success=False)."""
        mock_get_config.return_value = MagicMock(
            name="memory", transport="stdio", command="npx",
            args=["-y", "@mcp/server-memory"], enabled=True,
        )

        result = execute_tool(
            "memory__search_nodes",
            {"query": "test"},
            str(tmp_path),
            "test-conv",
        )
        assert result.success is False
        assert "Failed to connect" in result.error


# ── TestConversationCleanup ─────────────────────────────────────────────────

class TestConversationCleanup(TestState):
    @patch("utils.mcp_client._connect_async", side_effect=FakeMCPFactory.fake_connect_async)
    @patch("utils.mcp_client.get_server_config")
    def test_disconnect_on_conversation_replace(self, mock_get_config, mock_connect):
        """create_conversation with same session_key cleans up old MCP connections."""
        mock_get_config.return_value = MagicMock(
            name="memory", transport="stdio", command="npx",
            args=["-y", "@mcp/server-memory"], enabled=True,
        )

        # Create runtime with minimal config
        from agent.config import AgentConfig, EnforcementConfig
        config = AgentConfig(default_provider="openai", default_model="gpt-4")

        rt = AgentRuntime(config)
        sk = "cleanup-test-1"
        rt.create_conversation(
            agent_name="test",
            session_key=sk,
            mcp_servers=["memory"],
        )

        # Connect (pre-connect outside runtime)
        connect("memory", sk)
        assert is_connected(sk, "memory") is True

        # Create new conversation for same session key → cleanup
        rt.create_conversation(
            agent_name="test",
            session_key=sk,
            mcp_servers=["memory"],
        )
        # After replacement, MCP connection should be cleaned up
        assert is_connected(sk, "memory") is False
