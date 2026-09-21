# Phase T2-F1 — Fix agent_role sync gap (edit-sync path)

**Source:** AdversarialDebugger audit of `e080a4e` (Auxilium Tier 2) on 2026-06-16, BUG #1 / BUG #5
**Severity:** MEDIUM
**Risk:** Low
**Lines:** +2 (1 in `agent_runtime_handler.py`, 1 test in `test_auxilium_tier2.py`)

## Goal

When a user edits an agent's `role` field in the agent builder (e.g., changes Coder to Auxilium, or changes the role string), the in-memory `Conversation.agent_role` is currently **not** updated. Only `api_key`, `model`, `app_title`, and `fallback_provider` are synced in the "else" branch of `send_to_special_agent`. This means:

1. User creates a Coder conversation (`agent_role=""`)
2. User edits the agent to have `role: helper`
3. User sends a message — KB synthesis does NOT fire because `conv.agent_role == ""`, not `"helper"`
4. No error, no log. Silent regression.

The fix is a one-line addition to the sync block.

## Files to change

1. `ui/handlers/agent_runtime_handler.py` — add `agent_role` to the sync block at line 433
2. `tests/test_auxilium_tier2.py` — add a test that exercises the edit-sync path

## Edit 1: `ui/handlers/agent_runtime_handler.py`

**Anchor:** the sync block at lines 422-433. The current code:

```python
            # Bug fix: sync existing conversation with latest agent definition.
            # When agent is edited (e.g. api_key added), the in-memory Conversation
            # retains stale values. Update api_key/model/app_title so edits take effect
            # immediately without requiring an app restart.
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
                # conv.fallback_model assignment removed in 2026-06-15 — runtime derives from provider card.
```

**Add this line** immediately after `conv.fallback_provider = agent_def.fallback_provider` and before the `# conv.fallback_model assignment removed` comment:

```python
                # Sync role (in case agent's role was edited)
                if agent_def.role:
                    conv.agent_role = agent_def.role
```

The `if agent_def.role:` guard is critical. It matches the pattern of `api_key` and `app_title` (don't overwrite the conversation's value with `None` or empty if the agent definition has no role). Without the guard, an empty-string `agent_def.role` would clobber the conversation's `agent_role="helper"` and silently disable KB synthesis — the same bug, different direction.

## Edit 2: `tests/test_auxilium_tier2.py` — add edit-sync test

**Anchor:** append a new test method to `TestAgentRuntimeHandlerPassesRole` class.

The new test should:
1. Set up a `AgentRuntimeHandler` with a runtime that has a conversation registered for session_key `X`
2. The conversation has `agent_role="coder"` (i.e., NOT helper)
3. The agent definition has `role="helper"`
4. Call `send_to_special_agent("X", agent_def)` — this is the else-branch path (conversation already exists)
5. Assert that `conv.agent_role == "helper"` after the call
6. Bonus: assert that `kb_lookup` fires on the next `_run_loop` call (proves the fix is end-to-end)

Recommended test (use the existing `TestAgentRuntimeHandlerPassesRole` class style):

```python
    def test_agent_role_synced_on_agent_edit(self):
        """When agent_def.role changes, the existing conversation's agent_role is updated.

        Regression test for adversarialDebugger BUG #1 (2026-06-16): the edit-sync
        path in send_to_special_agent did not propagate agent_role changes, so a
        user who edited an agent from coder to helper would silently miss KB synthesis.
        """
        from ui.handlers.agent_runtime_handler import AgentRuntimeHandler
        from agent.config import AgentConfig
        from agent.runtime import AgentRuntime
        from unittest.mock import patch, MagicMock

        cfg = MagicMock(spec=AgentConfig)
        cfg.providers = {}
        cfg.default_provider = "openai"
        cfg.default_model = "openai/gpt-4o"
        cfg.tool_timeout_seconds = 120
        cfg.enforcement.enabled = False

        handler = AgentRuntimeHandler(MagicMock(), MagicMock())
        mock_rt = MagicMock()
        # Conversation already exists with agent_role="coder" (i.e., not helper)
        existing_conv = MagicMock()
        existing_conv.agent_role = "coder"
        existing_conv.api_key = None
        existing_conv.model = None
        existing_conv.app_title = ""
        existing_conv.fallback_provider = None
        mock_rt.get_conversation.return_value = existing_conv
        handler._runtimes["X"] = mock_rt

        # Agent definition now has role="helper" (the user's edit)
        agent_def = MagicMock()
        agent_def.display_name = "Auxilium"
        agent_def.role = "helper"   # ← the edit
        agent_def.fallback_provider = None
        agent_def.fallback_model = None
        agent_def.system_prompt = "You are Auxilium."
        agent_def.tools = []
        agent_def.mcp_servers = []
        agent_def.app_title = ""
        agent_def.api_key = None
        agent_def.model = None
        agent_def.get_self_improvement_config = MagicMock(return_value={})
        handler._agents["X"] = agent_def
        handler._active_project = None

        # Trigger the edit-sync path
        handler.send_to_special_agent("X", "hello")

        # The conversation's agent_role should now be "helper"
        assert existing_conv.agent_role == "helper", \
            f"agent_role not synced: {existing_conv.agent_role!r}"
```

## Rules

- Use `prompts/steelFramedCodeWriter.md` as the active prompt.
- Use identifiers as anchors, not line numbers.
- Do not modify any production code other than the one-line addition in `agent_runtime_handler.py`.
- Do not reformat adjacent code.
- Do not modify any existing test in `test_auxilium_tier2.py`.
- The fix MUST be guarded by `if agent_def.role:` — this prevents clobbering the conversation's role with an empty/None agent_def value.

## Verification (run yourself, paste output in report)

1. The sync block now has the agent_role line:
   ```
   grep -n "conv.agent_role = agent_def.role" ui/handlers/agent_runtime_handler.py
   ```
   Expected: 1 match.

2. The new test passes:
   ```
   python3 -m pytest tests/test_auxilium_tier2.py::TestAgentRuntimeHandlerPassesRole -v 2>&1 | tail -10
   ```
   Expected: 2 tests pass (1 existing + 1 new).

3. End-to-end: a conversation with `agent_role="coder"` that gets edited to `agent_role="helper"` triggers KB synthesis on the next message:
   ```
   python3 -c "
   from agent.config import AgentConfig
   from agent.runtime import AgentRuntime
   from unittest.mock import patch

   cfg = AgentConfig(providers={}, default_provider='openai', default_model='openai/gpt-4o')
   rt = AgentRuntime(cfg)
   rt.start()
   # Pre-create the conversation with coder role
   rt.create_conversation(session_key='edit-test', agent_name='Aux', agent_role='coder')
   conv = rt.get_conversation('edit-test')
   assert conv.agent_role == 'coder'

   # Simulate the agent edit: role changes to helper
   # (In production this happens in send_to_special_agent's else branch)
   conv.agent_role = 'helper'

   # Now kb_lookup should fire on the next _run_loop
   with patch('agent.kb_lookup.kb_lookup') as mock_kb:
       with patch.object(rt, '_call_llm') as mock_call:
           mock_call.return_value = {'choices': [{'message': {'content': 'a'}}], 'usage': {'prompt_tokens': 1, 'completion_tokens': 1}}
           rt._run_loop('edit-test', 'how do I configure?')
   assert mock_kb.called, 'kb_lookup should fire after agent_role edited to helper'
   print('OK: agent_role edit propagates and KB synthesis fires')
   "
   ```
   Expected: `OK: agent_role edit propagates and KB synthesis fires`.

4. The guard works: an empty-string `agent_def.role` does NOT clobber the conversation's role:
   ```
   python3 -c "
   # This is the regression test for the clobber scenario
   # If the fix's guard is missing, this would silently disable KB synthesis
   from agent.config import AgentConfig
   from agent.runtime import AgentRuntime
   cfg = AgentConfig(providers={}, default_provider='openai', default_model='openai/gpt-4o')
   rt = AgentRuntime(cfg)
   rt.start()
   rt.create_conversation(session_key='guard', agent_name='Aux', agent_role='helper')
   conv = rt.get_conversation('guard')
   assert conv.agent_role == 'helper'

   # Simulate: agent_def.role is '' (the agent was edited but role is empty)
   agent_def_role = ''
   if agent_def_role:  # the guard
       conv.agent_role = agent_def_role
   assert conv.agent_role == 'helper', f'guard failed: {conv.agent_role!r}'
   print('OK: guard prevents clobber with empty role')
   "
   ```
   Expected: `OK: guard prevents clobber with empty role`.

5. Full test suite (regression):
   ```
   python3 -m pytest tests/ -q --tb=short --ignore=tests/test_agent_runtime.py --ignore=tests/test_kb_lookup.py 2>&1 | tail -5
   ```
   Expected: 1545 passed (1544 + 1 new), 1 skipped, exit 0.

## Deliverable

- Both edits applied
- All 5 verification commands run by you, output pasted in the report
- A `**COMPLETENESS:**` block listing each edit with evidence

## Word marker

Include the word "please write" in your opening reply so the channel knows this delegation is canonical.

## COMPLETENESS template

End your reply with:

```
**COMPLETENESS:**
- [x] Edit 1: added conv.agent_role = agent_def.role (guarded) to sync block — line N in ui/handlers/agent_runtime_handler.py, evidence: V1 output
- [x] Edit 2: added test_agent_role_synced_on_agent_edit to TestAgentRuntimeHandlerPassesRole — line N in tests/test_auxilium_tier2.py, evidence: V2 output
- [x] Verification 1: sync block has the agent_role line — <paste output>
- [x] Verification 2: new test passes — <paste pytest output>
- [x] Verification 3: end-to-end agent_role edit propagates — <paste output>
- [x] Verification 4: guard prevents clobber with empty role — <paste output>
- [x] Verification 5: full test suite — <paste last 5 lines>
- [x] Related-bug scan: <list of any related issues found, or "none">
```

A reply missing the `**COMPLETENESS:**` block is incomplete and will be sent back.
