# Phase T2-F3 — Normalize agent_role comparison for case-insensitive KB synthesis

**Source:** AdversarialDebugger audit of `e080a4e` on 2026-06-16, BUG: agent_role case-sensitivity
**Severity:** LOW (silent regression for users who type non-lowercase roles)
**Risk:** Low
**Lines:** +2 (1 code, 1 test) in 1 production file

## Goal

The Tier 2 KB synthesis gate at `agent/runtime.py:1166` does a strict string comparison:
```python
if conv.agent_role == "helper":
```

If a user creates an auxilium agent with `role: "Helper"` (capitalized) or `role: " helper "` (whitespace), the comparison fails silently and KB synthesis is disabled. No error, no log.

Fix: normalize the comparison to `conv.agent_role.strip().lower() == "helper"`. This catches common typos and case variations.

**Scope of the fix:** ONLY the Tier 2 gate at line 1166. Do NOT modify `prompt_loader.py` (which has its own role checks at lines 131-132, 184, 198-202) — those are a different concern with different fix requirements. The agent builder UI sets the role via dropdown (already normalized to lowercase), so this fix is purely for programmatic mutation and accidental case variation.

## Files to change

1. `agent/runtime.py` — change the gate
2. `tests/test_auxilium_tier2.py` — add a test that exercises the case-insensitive comparison

## Edit 1: `agent/runtime.py`

**Anchor:** the gate at line 1166. Find this pattern:
```python
                if conv.agent_role == "helper":
```

**Replace with:**
```python
                # Case-insensitive match: "Helper", "HELPER", " helper " all work.
                if conv.agent_role.strip().lower() == "helper":
```

That's it. The 2-line change.

## Edit 2: `tests/test_auxilium_tier2.py` — add case-insensitive test

**Anchor:** append a new test method to `TestKBLookupFiresForAuxilium` class.

The new test should verify that various case/whitespace variations of "helper" all trigger KB synthesis:

```python
    def test_kb_lookup_called_for_case_insensitive_helper_role(self):
        """The Tier 2 gate matches role values case-insensitively, ignoring whitespace.

        Regression test for adversarialDebugger LOW bug (2026-06-16): a user
        who types 'Helper' or ' helper ' in agent_def.role would silently miss
        KB synthesis due to strict string equality.
        """
        from agent.config import AgentConfig
        from agent.runtime import AgentRuntime
        from unittest.mock import patch

        cfg = AgentConfig(providers={}, default_provider='openai', default_model='openai/gpt-4o')
        rt = AgentRuntime(cfg)
        rt.start()

        # Test multiple case/whitespace variations
        for weird_role in ["Helper", "HELPER", "helper ", " helper", "  HELPER  ", "HeLpEr"]:
            rt.create_conversation(
                session_key=f"k-{weird_role!r}",
                agent_name="Aux",
                agent_role=weird_role,
            )
            with patch("agent.kb_lookup.kb_lookup") as mock_kb:
                with patch.object(rt, "_call_llm") as mock_call:
                    mock_call.return_value = {
                        "choices": [{"message": {"content": "a"}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                    }
                    rt._run_loop(f"k-{weird_role!r}", "how do I configure?")
            assert mock_kb.called, (
                f"role={weird_role!r} should trigger KB synthesis (case-insensitive match)"
            )
```

## Rules

- Use `prompts/steelFramedCodeWriter.md` as the active prompt.
- Use identifiers as anchors, not line numbers.
- Do NOT modify `prompt_loader.py`. The fix is scoped to the Tier 2 gate only.
- Do NOT modify the `Conversation.agent_role` field — the stored value is the raw user input. The normalization happens at the gate.
- Do NOT add a helper function for the normalization. The `strip().lower()` is a 2-method chain, not a pattern that needs abstraction.
- Do NOT reformat adjacent code.

## Verification (run yourself, paste output in report)

1. The gate is now case-insensitive:
   ```
   grep -n "conv.agent_role.strip" agent/runtime.py
   ```
   Expected: 1 match (the gate, with the comment).

2. The old strict comparison is gone:
   ```
   grep -n "conv.agent_role == \"helper\"" agent/runtime.py
   ```
   Expected: 0 matches (the strict comparison is replaced).

3. The new test passes:
   ```
   python3 -m pytest tests/test_auxilium_tier2.py::TestKBLookupFiresForAuxilium -v 2>&1 | tail -10
   ```
   Expected: 4 tests pass (3 existing + 1 new).

4. End-to-end case-insensitive match works for all variations:
   ```
   python3 -c "
   from agent.config import AgentConfig
   from agent.runtime import AgentRuntime
   from unittest.mock import patch

   cfg = AgentConfig(providers={}, default_provider='openai', default_model='openai/gpt-4o')
   rt = AgentRuntime(cfg)
   rt.start()

   for weird_role in ['Helper', 'HELPER', 'helper ', ' helper', '  HELPER  ', 'HeLpEr']:
       rt.create_conversation(session_key=f'k-{weird_role!r}', agent_name='Aux', agent_role=weird_role)
       with patch('agent.kb_lookup.kb_lookup') as mock_kb:
           with patch.object(rt, '_call_llm') as mock_call:
               mock_call.return_value = {'choices': [{'message': {'content': 'a'}}], 'usage': {'prompt_tokens': 1, 'completion_tokens': 1}}
               rt._run_loop(f'k-{weird_role!r}', 'test')
       status = 'OK' if mock_kb.called else 'FAIL'
       print(f'role={weird_role!r:20s} -> {status}')
   "
   ```
   Expected: all 6 variations show `OK`.

5. Non-helper roles still don't trigger KB synthesis (the fix doesn't break the negative case):
   ```
   python3 -c "
   from agent.config import AgentConfig
   from agent.runtime import AgentRuntime
   from unittest.mock import patch

   cfg = AgentConfig(providers={}, default_provider='openai', default_model='openai/gpt-4o')
   rt = AgentRuntime(cfg)
   rt.start()

   for non_helper in ['coder', 'debugger', 'Coder', '  coder  ', '']:
       rt.create_conversation(session_key=f'k-{non_helper!r}', agent_name='X', agent_role=non_helper)
       with patch('agent.kb_lookup.kb_lookup') as mock_kb:
           with patch.object(rt, '_call_llm') as mock_call:
               mock_call.return_value = {'choices': [{'message': {'content': 'a'}}], 'usage': {'prompt_tokens': 1, 'completion_tokens': 1}}
               rt._run_loop(f'k-{non_helper!r}', 'test')
       status = 'OK' if not mock_kb.called else 'FAIL'
       print(f'role={non_helper!r:20s} -> {status}')
   "
   ```
   Expected: all 5 non-helper variations show `OK` (kb_lookup NOT called).

6. Full test suite:
   ```
   python3 -m pytest tests/ -q --tb=short --ignore=tests/test_agent_runtime.py --ignore=tests/test_kb_lookup.py 2>&1 | tail -5
   ```
   Expected: 1547 passed (1546 + 1 new), 1 skipped, exit 0.

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
- [x] Edit 1: changed gate to case-insensitive — line N in agent/runtime.py, evidence: V1 + V2 output
- [x] Edit 2: added test_kb_lookup_called_for_case_insensitive_helper_role — line N in tests/test_auxilium_tier2.py, evidence: V3 output
- [x] Verification 1: gate is case-insensitive — <paste output>
- [x] Verification 2: old strict comparison is gone — <paste output>
- [x] Verification 3: new test passes — <paste pytest output>
- [x] Verification 4: case-insensitive match works — <paste output>
- [x] Verification 5: non-helper roles don't trigger — <paste output>
- [x] Verification 6: full test suite — <paste last 5 lines>
- [x] Related-bug scan: <list of any related issues found, or "none">
```
