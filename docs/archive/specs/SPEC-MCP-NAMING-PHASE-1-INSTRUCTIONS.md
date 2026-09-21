# PHASE 1 — MCP Tool Name Sanitization (Wire-Safe Namespacing)

**Spec:** `docs/specs/spec-mcp-tool-name-sanitization.md`
**Files to change:** `utils/mcp_client.py`, `agent/tools.py`, `tests/test_mcp_tool_naming.py` (NEW)

This is a single-phase implementation. The 3 files are tightly coupled — the wire-name functions must exist before `execute_tool` can use them, and the test must cover both.

---

## FIX 1 — Add `_to_wire_name` and `_from_wire_name` to `utils/mcp_client.py`

**Insertion point:** After the `_sanitize_tool_description` function (which ends with `return sanitized` at line 486), before the `def get_tools_for_api(` at line 489.

**Exact code to insert** (between line 487's blank line and line 489's `def get_tools_for_api`):

```python
# Wire-name separator: replaces "/" in MCP namespaced tool names.
# Must be provider-safe (matches ^[a-zA-Z0-9_-]{1,128}$) and not collide
# with built-in tool names (none of which contain "__").
_WIRE_NAME_SEPARATOR = "__"


def _to_wire_name(server_name: str, tool_name: str) -> str:
    """Convert an MCP (server_name, tool_name) pair to a provider-safe wire name.

    Provider APIs (Anthropic, OpenAI, Poolside) reject tool names containing "/".
    The internal namespacing uses "server/tool" for routing, but the wire
    format must use only [a-zA-Z0-9_-]. This function produces "server__tool".

    Args:
        server_name: MCP server name (e.g. "memory"). Must not contain "__"
                    or the separator. mcp_config.py already validates that
                    server names do not contain "/".
        tool_name: MCP tool name (e.g. "create_entities"). May contain "__"
                  internally — the split in _from_wire_name uses the FIRST
                  separator occurrence, preserving it.

    Returns:
        Provider-safe wire name (e.g. "memory__create_entities").
    """
    return f"{server_name}{_WIRE_NAME_SEPARATOR}{tool_name}"


def _from_wire_name(wire_name: str) -> tuple[str, str] | None:
    """Split a wire name back into (server_name, tool_name).

    Splits on the FIRST separator occurrence, so tool names containing "__"
    are preserved intact after the split.

    Args:
        wire_name: The wire-format name (e.g. "memory__create_entities").

    Returns:
        (server_name, tool_name) if the name contains the separator, else None.
        None means this is NOT an MCP wire name — the caller should treat it
        as a built-in tool name.
    """
    if _WIRE_NAME_SEPARATOR not in wire_name:
        return None
    server, _, tool = wire_name.partition(_WIRE_NAME_SEPARATOR)
    if not server or not tool:
        return None
    return server, tool


```

---

## FIX 2 — Rewrite `get_tools_for_api` namespacing in `utils/mcp_client.py`

**Current code at line 520** (inside the `for tool in server_tools:` loop in `get_tools_for_api`):

```python
                namespaced = f"{server_name}/{tool.name}"
                raw_desc = tool.description or f"MCP: {tool.name}"
                sanitized_desc = _sanitize_tool_description(raw_desc)
                func_dict = {
                    "type": "function",
                    "function": {
                        "name": namespaced,
                        "description": sanitized_desc or f"MCP: {tool.name}",
                        "parameters": tool.parameters or {"type": "object", "properties": {}},
                    },
                }
```

**New code** (replace the `namespaced = ...` line; keep everything else identical):

```python
                wire_name = _to_wire_name(server_name, tool.name)
                raw_desc = tool.description or f"MCP: {tool.name}"
                sanitized_desc = _sanitize_tool_description(raw_desc)
                func_dict = {
                    "type": "function",
                    "function": {
                        "name": wire_name,
                        "description": sanitized_desc or f"MCP: {tool.name}",
                        "parameters": tool.parameters or {"type": "object", "properties": {}},
                    },
                }
```

Only TWO things change: the variable name (`namespaced` → `wire_name`) and the value (`f"{server_name}/{tool.name}"` → `_to_wire_name(server_name, tool.name)`). The `"name": namespaced` key on the next line becomes `"name": wire_name`.

---

## FIX 3 — Update `execute_tool` MCP routing in `agent/tools.py`

**Current code at lines 1189-1196:**

```python
    # Phase B: MCP tool routing — namespaced tools like "fetch/fetch"
    if "/" in name:
        server_name, _, tool_name = name.partition("/")
        if not server_name or not tool_name:
            return ToolResult(
                success=False,
                error=f"Invalid MCP tool name '{name}': expected 'server/tool' format",
            )
```

**New code (replaces lines 1189-1196):**

```python
    # MCP tool routing. Wire names use "__" as the separator (provider-safe).
    from utils.mcp_client import _from_wire_name
    server_tool = _from_wire_name(name)
    if server_tool is None:
        return ToolResult(
            success=False,
            error=f"Unknown tool: {name}",
        )
    server_name, tool_name = server_tool
```

**Important:** The code immediately AFTER this block (lines 1197+) stays UNCHANGED. It uses `server_name` and `tool_name` variables which are now set by the new code. Specifically, this block remains untouched:

```python
        # Route to MCP client
        try:
            from utils.mcp_client import call_tool as mcp_call_tool, is_connected
            ...
```

Note: the `try:` block at line 1197 is currently indented under the `if "/" in name:` block. After the change, it must be indented under the new `if server_tool is None: ... else` flow. **Look at the indentation carefully** — the new code sets `server_name` and `tool_name` at the same indentation level as the old `if "/" in name:` block, so the `try:` block that follows stays at the same indentation. If the old code had the `try:` indented inside the `if`, the new code keeps the same structure: the routing code runs when `server_tool is not None`.

---

## FIX 4 — Create `tests/test_mcp_tool_naming.py`

**New file** covering the wire-name helpers, the tool-merge, and the routing round-trip.

```python
# tests/test_mcp_tool_naming.py
# Tests for MCP tool name sanitization (wire-safe namespacing).
#
# Verifies that:
# 1. _to_wire_name / _from_wire_name produce provider-safe names
# 2. get_tools_for_api emits no "/" in tool names
# 3. execute_tool routes wire-format names correctly
# 4. execute_tool rejects legacy "/" format (clean cut)

import pytest
from unittest.mock import patch, MagicMock

from utils.mcp_client import _to_wire_name, _from_wire_name, MCPToolDefinition


class TestToWireName:

    def test_basic(self):
        assert _to_wire_name("memory", "create_entities") == "memory__create_entities"

    def test_no_slash_in_output(self):
        result = _to_wire_name("fetch", "fetch_url")
        assert "/" not in result

    def test_matches_provider_pattern(self):
        import re
        result = _to_wire_name("memory", "search_nodes")
        # Must match ^[a-zA-Z0-9_-]{1,128}$
        assert re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", result), (
            f"Wire name {result!r} does not match provider pattern"
        )

    def test_empty_server(self):
        # Edge case — produces "__tool" (defensive; server names always non-empty)
        assert _to_wire_name("", "tool") == "__tool"

    def test_empty_tool(self):
        assert _to_wire_name("memory", "") == "memory__"


class TestFromWireName:

    def test_basic(self):
        assert _from_wire_name("memory__create_entities") == ("memory", "create_entities")

    def test_built_in_tool_returns_none(self):
        assert _from_wire_name("read_file") is None

    def test_empty_string_returns_none(self):
        assert _from_wire_name("") is None

    def test_double_underscore_in_tool_name(self):
        # Split on FIRST "__" only — tool name preserved intact
        assert _from_wire_name("memory__create__entities") == ("memory", "create__entities")

    def test_empty_server_returns_none(self):
        assert _from_wire_name("__tool") is None

    def test_empty_tool_returns_none(self):
        assert _from_wire_name("memory__") is None

    def test_no_separator_returns_none(self):
        assert _from_wire_name("justaname") is None

    def test_round_trip(self):
        wire = _to_wire_name("memory", "search_nodes")
        assert _from_wire_name(wire) == ("memory", "search_nodes")


class TestGetToolsForApiWireNames:
    """Verify get_tools_for_api produces provider-safe tool names."""

    def test_no_slash_in_tool_names(self):
        """get_tools_for_api must not emit "/" in any tool name."""
        from utils.mcp_client import get_tools_for_api, _conversations, _tools_cache, _MCPLoopThread

        # Mock discover_tools to return fake tool defs without actual MCP connection
        fake_tools = [
            MCPToolDefinition(
                name="create_entities",
                description="Create entities in the knowledge graph",
                parameters={"type": "object", "properties": {}},
                server_name="memory",
            ),
            MCPToolDefinition(
                name="search_nodes",
                description="Search nodes in the knowledge graph",
                parameters={"type": "object", "properties": {}},
                server_name="memory",
            ),
        ]

        # Clear state to avoid interference
        _conversations.clear()
        _tools_cache.clear()
        for k in list(_MCPLoopThread._instances.keys()):
            inst = _MCPLoopThread._instances.pop(k)
            try:
                inst.stop()
            except Exception:
                pass

        with patch("utils.mcp_client.connect"), \
             patch("utils.mcp_client.discover_tools", return_value=fake_tools):
            tools = get_tools_for_api(["memory"])

        assert len(tools) == 2
        for t in tools:
            name = t["function"]["name"]
            assert "/" not in name, f"Tool name {name!r} contains '/'"
            assert "__" in name, f"Tool name {name!r} missing wire separator"

    def test_wire_names_match_provider_pattern(self):
        """All emitted tool names must match ^[a-zA-Z0-9_-]{1,128}$."""
        import re
        from utils.mcp_client import get_tools_for_api, _conversations, _tools_cache, _MCPLoopThread

        fake_tools = [
            MCPToolDefinition(
                name="create_entities",
                description="Create entities",
                parameters={"type": "object", "properties": {}},
                server_name="memory",
            ),
        ]

        _conversations.clear()
        _tools_cache.clear()
        for k in list(_MCPLoopThread._instances.keys()):
            inst = _MCPLoopThread._instances.pop(k)
            try:
                inst.stop()
            except Exception:
                pass

        with patch("utils.mcp_client.connect"), \
             patch("utils.mcp_client.discover_tools", return_value=fake_tools):
            tools = get_tools_for_api(["memory"])

        for t in tools:
            name = t["function"]["name"]
            assert re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", name), (
                f"Tool name {name!r} does not match provider pattern"
            )


class TestExecuteToolRouting:
    """Verify execute_tool routes wire-format names and rejects legacy format."""

    def test_wire_name_routes_to_mcp(self):
        """execute_tool('memory__search_nodes', ...) should route to MCP."""
        from agent.tools import execute_tool

        mock_result = MagicMock()
        mock_result.success = True
        mock_result.output = "search results"
        mock_result.error = None
        mock_result.duration_ms = 5

        with patch("utils.mcp_client.is_connected", return_value=True), \
             patch("utils.mcp_client.call_tool", return_value=mock_result):
            result = execute_tool(
                "memory__search_nodes",
                {"query": "test"},
                project_path="/tmp",
            )

        assert result.success is True
        assert result.output == "search results"

    def test_legacy_slash_format_rejected(self):
        """execute_tool('memory/search_nodes', ...) must return Unknown tool."""
        from agent.tools import execute_tool

        result = execute_tool(
            "memory/search_nodes",
            {"query": "test"},
            project_path="/tmp",
        )
        assert result.success is False
        assert "Unknown tool" in result.error

    def test_built_in_tool_still_works(self):
        """execute_tool('read_file', ...) must NOT be affected by MCP routing."""
        from agent.tools import execute_tool

        # read_file on a nonexistent path returns failure but NOT "Unknown tool"
        result = execute_tool(
            "read_file",
            {"path": "/nonexistent/path/that/does/not/exist.py"},
            project_path="/tmp",
        )
        assert result.success is False
        assert "Unknown tool" not in (result.error or "")
```

---

## Rules

- Use the `steelFramedCodeWriter` prompt at `prompts/steelFramedCodeWriter.md`.
- Make ONLY the changes described above. Do not refactor, rename, or reformat anything else.
- **Read each file before editing it** (steelFramedCodeWriter Rule 1).
- **Indentation matters in FIX 3.** The `try:` block at line 1197 (the MCP routing execution) must remain at the same indentation relative to the new code. The new code sets `server_name` and `tool_name`, then falls through to the `try:` block. If the old `if "/" in name:` block indented the `try:`, the new `if server_tool is None: return ...` followed by `server_name, tool_name = server_tool` must keep the `try:` at the same level.
- Do NOT touch `agent/runtime.py`, `utils/mcp_config.py`, or `models/conversation.py`.

## Verification commands (run these, paste the output)

```bash
cd /path/to/projects/crabcakes

# 1. Confirm new functions exist
python3 -c "
from utils.mcp_client import _to_wire_name, _from_wire_name
assert _to_wire_name('memory', 'create_entities') == 'memory__create_entities'
assert _from_wire_name('memory__create_entities') == ('memory', 'create_entities')
assert _from_wire_name('read_file') is None
print('OK: wire-name helpers work')
"

# 2. Confirm get_tools_for_api emits no slashes (unit test)
python3 -m pytest tests/test_mcp_tool_naming.py::TestGetToolsForApiWireNames -v

# 3. Confirm execute_tool routes wire names and rejects legacy
python3 -m pytest tests/test_mcp_tool_naming.py::TestExecuteToolRouting -v

# 4. Full new test file
python3 -m pytest tests/test_mcp_tool_naming.py -v

# 5. Regression — existing MCP tests
python3 -m pytest tests/test_mcp_client.py tests/test_mcp_integration.py -v

# 6. Regression — tools tests
python3 -m pytest tests/test_tools.py -v

# 7. Pattern sweep — no remaining "/" namespacing in get_tools_for_api
grep -n 'server_name}/' utils/mcp_client.py
# Expected: 0 matches

# 8. Pattern sweep — execute_tool uses _from_wire_name
grep -n '_from_wire_name\|partition.*"/"' agent/tools.py
# Expected: _from_wire_name present; partition("/") gone from execute_tool
```

## Deliverables (COMPLETENESS checklist required)

When done, report:
1. Files changed with line numbers
2. Full output of all 8 verification commands above
3. `git diff utils/mcp_client.py agent/tools.py` output (the actual changes)
4. COMPLETENESS checklist:
```
COMPLETENESS:
- [x/not done] Fix 1: _to_wire_name + _from_wire_name added to mcp_client.py — evidence: (command 1 output)
- [x/not done] Fix 2: get_tools_for_api uses _to_wire_name (no "/") — evidence: (command 2 output)
- [x/not done] Fix 3: execute_tool uses _from_wire_name for routing — evidence: (command 3 output)
- [x/not done] Fix 4: test_mcp_tool_naming.py created — evidence: (command 4 output)
- [x/not done] Existing MCP tests pass — evidence: (command 5 output)
- [x/not done] Existing tools tests pass — evidence: (command 6 output)
- [x/not done] Pattern sweep: no "/" namespacing — evidence: (command 7 output)
- [x/not done] Pattern sweep: _from_wire_name present — evidence: (command 8 output)
```
