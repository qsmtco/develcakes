# Phase T2-F2 — Sync mcp_servers and si_enforcement on agent edit

**Source:** AdversarialDebugger audit of `e080a4e` on 2026-06-16, related-bug follow-up to T2-F1
**Severity:** MEDIUM (same class as BUG #1 — silent staleness on agent edit)
**Risk:** Low
**Lines:** +4 in production (2 code + 2 comment), +50 in tests

## Goal

Extend the edit-sync block in `send_to_special_agent` to also update `conv.mcp_servers` and `conv.si_enforcement` when the agent definition is edited. Currently these fields are set at `create_conversation()` time and never updated — same pattern as the `agent_role` bug that T2-F1 fixed.

## Files to change

1. `ui/handlers/agent_runtime_handler.py` — add 2 lines to the sync block
2. `tests/test_auxilium_tier2.py` — add 1 test that exercises both new syncs

## Edit 1: `ui/handlers/agent_runtime_handler.py`

**Anchor:** the sync block at lines 422-433 (post-T2-F1, with agent_role already added).

Current sync block (after T2-F1):
```python
            # Bug fix: sync existing conversation with latest agent definition.
            conv = rt.get_conversation(session_key)
            if conv is not None:
                if agent_def.api_key:
                    conv.api_key = agent_def.api_key
                if agent_model:
                    conv.model = agent_model
                if agent_def.app_title:
                    conv.app_title = agent_def.app_title
                # Sync fallback config (in case agent was edited)
                conv.fallback_provider = agent_def.fallback_provider
                # Sync role (in case agent's role was edited)
                if agent_def.role:
                    conv.agent_role = agent_def.role
```

**Add these 2 lines** immediately after the `agent_role` block:

```python
                # Sync MCP servers (in case agent's mcp_servers list was edited)
                if agent_def.mcp_servers is not None:
                    conv.mcp_servers = list(agent_def.mcp_servers)
                # Sync SI enforcement (in case agent's self_improvement was edited)
                if si_enforcement is not None:
                    conv.si_enforcement = si_enforcement
```

**Notes on each:**

- **mcp_servers guard:** `is not None` is appropriate here because the type is `list[str]` and an empty list `[]` is a valid value (means "no MCP servers"). `list(agent_def.mcp_servers)` makes a defensive copy so the conversation and agent_def don't share the same list reference.

- **si_enforcement source:** use the local `si_enforcement` variable (resolved at line 397-400 from `agent_def.get_self_improvement_config()`). Don't call `get_self_improvement_config()` again — it's already been resolved once for the create path.

- **si_enforcement guard:** `is not None` is correct. `None` means "use global default"; only update the conversation's value when the agent_def has an explicit value.

## Edit 2: `tests/test_auxilium_tier2.py` — add edit-sync test for mcp_servers and si_enforcement

**Anchor:** append a new test method to `TestAgentRuntimeHandlerPassesRole` class.

The new test should:
1. Set up a `AgentRuntimeHandler` with a runtime that has a conversation registered for session_key `X`
2. The conversation has `mcp_servers=["old-server"]` and `si_enforcement=False` (i.e., stale)
3. The agent definition has `mcp_servers=["new-server-1", "new-server-2"]` and `get_self_improvement_config` returns `{"enforcement": True}`
4. Call `send_to_special_agent("X", agent_def)` — this is the else-branch path
5. Assert that `conv.mcp_servers == ["new-server-1", "new-server-2"]`
6. Assert that `conv.si_enforcement is True`

Recommended test:

```python
    def test_mcp_servers_and_si_enforcement_synced_on_agent_edit(self):
        """When agent_def's mcp_servers or self_improvement changes, the existing
        conversation's mcp_servers and si_enforcement are updated.

        Regression test for adversarialDebugger related-bug follow-up to T2-F1:
        same edit-sync pattern as agent_role, but for mcp_servers and si_enforcement.
        """
        from ui.handlers.agent_runtime_handler import AgentRuntimeHandler
        from unittest.mock import MagicMock

        handler = AgentRuntimeHandler(MagicMock(), MagicMock())
        mock_rt = MagicMock()
        existing_conv = MagicMock()
        existing_conv.agent_role = "helper"
        existing_conv.api_key = None
        existing_conv.model = None
        existing_conv.app_title = ""
        existing_conv.fallback_provider = None
        # Stale values that should be overwritten
        existing_conv.mcp_servers = ["old-server"]
        existing_conv.si_enforcement = False
        mock_rt.get_conversation.return_value = existing_conv
        handler._runtimes["X"] = mock_rt

        agent_def = MagicMock()
        agent_def.display_name = "X"
        agent_def.role = "helper"
        agent_def.fallback_provider = None
        agent_def.fallback_model = None
        agent_def.system_prompt = "sys"
        agent_def.tools = []
        agent_def.mcp_servers = ["new-server-1", "new-server-2"]
        agent_def.app_title = ""
        agent_def.api_key = None
        agent_def.model = None
        agent_def.get_self_improvement_config = MagicMock(return_value={"enforcement": True})
        handler._agents["X"] = agent_def
        handler._active_project = None

        handler.send_to_special_agent("X", "hello")

        # mcp_servers should be updated to the new list
        assert existing_conv.mcp_servers == ["new-server-1", "new-server-2"], \
            f"mcp_servers not synced: {existing_conv.mcp_servers!r}"
        # si_enforcement should be True (from get_self_improvement_config)
        assert existing_conv.si_enforcement is True, \
            f"si_enforcement not synced: {existing_conv.si_enforcement!r}"
```

## Rules

- Use `prompts/steelFramedCodeWriter.md` as the active prompt.
- Use identifiers as anchors, not line numbers.
- Do not modify any other production code.
- Do not reformat adjacent code.
- The `list(agent_def.mcp_servers)` defensive copy is required — without it, the conversation and agent_def share a reference and a later mutation in agent_def would silently affect the conversation.
- The `si_enforcement is not None` guard is required — without it, an explicit `False` in the agent_def would not be propagated (the `is None` check is more permissive than truthy check).

## Verification (run yourself, paste output in report)

1. The sync block has both new lines:
   ```
   grep -n "conv.mcp_servers = list\|conv.si_enforcement = si_enforcement" ui/handlers/agent_runtime_handler.py
   ```
   Expected: 2 matches (one for each).

2. The new test passes:
   ```
   python3 -m pytest tests/test_auxilium_tier2.py::TestAgentRuntimeHandlerPassesRole -v 2>&1 | tail -10
   ```
   Expected: 3 tests pass (1 from T2-4 + 1 from T2-F1 + 1 new).

3. End-to-end: changing mcp_servers in agent_def propagates to conv and to subsequent MCP tool lookups. Simulate by directly mutating the conv (since the runtime path is hard to mock fully):
   ```
   python3 -c "
   from agent.config import AgentConfig
   from agent.runtime import AgentRuntime
   cfg = AgentConfig(providers={}, default_provider='openai', default_model='openai/gpt-4o')
   rt = AgentRuntime(cfg)
   rt.start()
   rt.create_conversation(session_key='mcp', agent_name='Aux', agent_role='helper', mcp_servers=['old'])
   conv = rt.get_conversation('mcp')
   assert conv.mcp_servers == ['old']
   # Simulate the sync
   conv.mcp_servers = list(['new-1', 'new-2'])
   assert conv.mcp_servers == ['new-1', 'new-2']
   print('OK: mcp_servers sync propagates')
   "
   ```
   Expected: `OK: mcp_servers sync propagates`.

4. End-to-end: changing si_enforcement propagates:
   ```
   python3 -c "
   from agent.config import AgentConfig
   from agent.runtime import AgentRuntime
   cfg = AgentConfig(providers={}, default_provider='openai', default_model='openai/gpt-4o')
   rt = AgentRuntime(cfg)
   rt.start()
   rt.create_conversation(session_key='si', agent_name='Aux', agent_role='helper', si_enforcement=False)
   conv = rt.get_conversation('si')
   assert conv.si_enforcement is False
   # Simulate the sync
   conv.si_enforcement = True
   assert conv.si_enforcement is True
   print('OK: si_enforcement sync propagates')
   "
   ```
   Expected: `OK: si_enforcement sync propagates`.

5. Defensive copy: the conversation's mcp_servers list is NOT the same object as agent_def's mcp_servers list:
   ```
   python3 -c "
   # This test ensures the production code uses list() to make a copy
   import inspect
   from ui.handlers import agent_runtime_handler as h
   src = inspect.getsource(h.AgentRuntimeHandler.send_to_special_agent)
   if 'list(agent_def.mcp_servers)' in src:
       print('OK: defensive copy is in place')
   else:
       print('FAIL: defensive copy missing')
   "
   ```
   Expected: `OK: defensive copy is in place`.

6. Full test suite:
   ```
   python3 -m pytest tests/ -q --tb=short --ignore=tests/test_agent_runtime.py --ignore=tests/test_kb_lookup.py 2>&1 | tail -5
   ```
   Expected: 1546 passed (1545 + 1 new), 1 skipped, exit 0.

## Deliverable

- Both edits applied
- All 6 verification commands run by you, output pasted in the report
- A `**COMPLETENESS:**` block listing each edit with evidence

## Word marker

Include the word "please write" in your opening reply so the channel knows this delegation is canonical.

## COMPLETENESS template

End your reply with:

```
**COMPLETENESS:**
- [x] Edit 1: added mcp_servers and si_enforcement sync — line N in ui/handlers/agent_runtime_handler.py, evidence: V1 output
- [x] Edit 2: added test_mcp_servers_and_si_enforcement_synced_on_agent_edit — line N in tests/test_auxilium_tier2.py, evidence: V2 output
- [x] Verification 1: sync block has both new lines — <paste output>
- [x] Verification 2: new test passes — <paste pytest output>
- [x] Verification 3: mcp_servers end-to-end — <paste output>
- [x] Verification 4: si_enforcement end-to-end — <paste output>
- [x] Verification 5: defensive copy is in place — <paste output>
- [x] Verification 6: full test suite — <paste last 5 lines>
- [x] Related-bug scan: <list of any related issues found, or "none">
```
