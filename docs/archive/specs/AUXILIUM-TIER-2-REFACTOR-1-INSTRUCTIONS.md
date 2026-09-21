# Phase T2-R1 — Refactor duplicate injection logic

**Source:** Auxilium Tier 2 post-mortem §3.1 (duplicate code flagged)
**Spec reference:** `docs/post-mortems/2026-06-16-AUXILIUM-TIER-2-KB-SYNTHESIS-POST-MORTEM.md` §3.1
**Target:** main
**Risk:** Low (pure refactor — behavior must be identical to the inline code)
**Lines:** -10 inline, +3 method call, +1 test (~30 lines)

## Goal

Replace the inline message-injection code at `agent/runtime.py:1258-1268` (in the KB fallback chain) with a single call to the existing `AgentRuntime._inject_kb_context` method. After the refactor, the only place KB chunks get prepended to a user message is inside `_inject_kb_context` — no duplicates.

## Background

`AgentRuntime._inject_kb_context` was added in Phase T2-2 at `agent/runtime.py:1078-1106`. It does exactly what the inline code in the KB fallback chain does: find the last user message, prepend `kb_context` to its content, return a new list.

The two code paths are:

- **Primary call (Tier 2):** fires on every auxilium message, calls `_inject_kb_context` (the new method).
- **Fallback call (KB chain):** fires only when primary returns `KB_OUT_OF_SCOPE && fallback_provider && !_fallback_attempted`. Currently has its own inline injection logic.

Both use the same format string `f"{kb_context}\n\nUser question: {original_content}"`. If anyone ever changes that format, they have to change it in two places. The refactor collapses this to one.

## Files to change

1. `agent/runtime.py` — replace the inline injection in the KB fallback chain
2. `tests/test_auxilium_tier2.py` — add one test that exercises the fallback path with the refactored call

## Edit 1: Replace inline injection in the KB fallback chain

**Anchor:** find the `if kb_context:` block inside `_run_loop`, in the section labeled "Inject KB context into messages for fallback LLM" (or similar). The pattern to find:

```python
                            # Inject KB context into messages for fallback LLM
                            if kb_context:
                                messages_with_context = list(messages)
                                for i in range(len(messages_with_context) - 1, -1, -1):
                                    if messages_with_context[i].get("role") == "user":
                                        messages_with_context[i] = {
                                            "role": "user",
                                            "content": f"{kb_context}\n\nUser question: {messages_with_context[i]['content']}",
                                        }
                                        break
                                fb_response = self._call_llm(session_key, messages_with_context, tools)
                            else:
                                fb_response = self._call_llm(session_key, messages, tools)
```

**Replace with this single call (or equivalent 2-line version):**

```python
                            # Inject KB context into fallback LLM call. Uses the
                            # same helper as the Tier 2 primary-call path so
                            # both paths share one format string.
                            messages_with_context = self._inject_kb_context(messages, kb_context, text)
                            fb_response = self._call_llm(session_key, messages_with_context, tools)
```

**Why this is correct:** `_inject_kb_context` has a defensive return — if `kb_context` is empty/None, it returns `messages` unchanged. So a single call replaces both the `if kb_context:` branch and the `else:` branch with no behavior change.

**The `text` parameter:** the third argument to `_inject_kb_context` is the current user message text. In `_run_loop`, the `text` variable is the parameter that was passed to the function (e.g., the user's typed message). The inline code used `messages_with_context[i]['content']` which is the same thing — the last user message's content is the same as `text` in this context. Pass `text`.

## Edit 2: Add a test for the fallback path

**Anchor:** append a new test method to `TestKBContextInjection` in `tests/test_auxilium_tier2.py`. The existing class has 3 tests for the primary call path. Add a 4th for the fallback path.

The test should:
1. Set up a runtime with an auxilium conversation that has a `fallback_provider` configured
2. Mock `_call_llm` to return `KB_OUT_OF_SCOPE` on the first call (triggering the fallback chain)
3. Mock `kb_lookup` to return a chunk
4. Assert that the **second** `_call_llm` call (the fallback call) receives messages with KB context prepended to the last user message

Reference for the pattern: look at how `test_kb_context_injected_into_primary_call` mocks `_call_llm` with `side_effect`. You'll need `side_effect=[kb_out_of_scope_response, fallback_response]` to return the out-of-scope response on the first call and a normal response on the second.

If the test is hard to set up because of internal helpers, here's a simpler version that exercises just the injection: call `_inject_kb_context` directly with a list that has the fallback's `kb_context` format and confirm the last user message has the prepended content. This is a unit test of the helper, not an integration test of the fallback chain.

**Recommended:** do the simpler unit test first (5 minutes, no setup), then if time permits do the integration test. The unit test is sufficient to prove the helper works for both call sites; the integration test proves the wiring in `_run_loop` is correct.

Suggested new test (append to `TestKBContextInjection`):

```python
def test_inject_kb_context_used_by_fallback_path(self):
    """The KB fallback chain uses the same _inject_kb_context helper as Tier 2.

    Verifies the helper handles the fallback-chain case: prepending KB context
    to a list where the last user message is the most recent turn.
    """
    rt, sk = _make_runtime_with_conv(agent_role="helper")
    messages = [
        {"role": "system", "content": "You are Auxilium."},
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "second question"},
    ]
    # Simulate the fallback path: pre-formatted KB context + current user text
    kb_context = "[KB Context]\nSource: knowledge/install.md\nGTK4 install on Ubuntu..."
    current_text = "second question"
    out = rt._inject_kb_context(messages, kb_context, current_text)
    # Same assertions as the primary-call test
    user_msgs = [m for m in out if m.get("role") == "user"]
    assert len(user_msgs) == 2
    last_user = user_msgs[-1]
    assert "GTK4 install on Ubuntu" in last_user["content"]
    assert "second question" in last_user["content"]
    # First user message should be untouched
    assert user_msgs[0] is not messages[1]  # ...wait, this is wrong, fix it
```

Actually the last assertion is wrong — let me give the clean version:

```python
    def test_inject_kb_context_used_by_fallback_path(self):
        """The KB fallback chain uses the same _inject_kb_context helper as Tier 2."""
        rt, sk = _make_runtime_with_conv(agent_role="helper")
        messages = [
            {"role": "system", "content": "You are Auxilium."},
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": "first answer"},
            {"role": "user", "content": "second question"},
        ]
        kb_context = "[KB Context]\nSource: knowledge/install.md\nGTK4 install on Ubuntu..."
        current_text = "second question"
        out = rt._inject_kb_context(messages, kb_context, current_text)
        # The output is a new list (defensive copy)
        assert out is not messages
        # The system message is the same object (no mutation)
        assert out[0] is messages[0]
        # The first user message is the same object (only the last is modified)
        assert out[1] is messages[1]
        # The assistant message is the same object
        assert out[2] is messages[2]
        # The last user message is a new dict with KB context prepended
        assert out[3] is not messages[3]
        assert "GTK4 install on Ubuntu" in out[3]["content"]
        assert "second question" in out[3]["content"]
        # Specifically, the format is "{kb_context}\n\nUser question: {original}"
        assert out[3]["content"].startswith("[KB Context]")
        assert "User question: second question" in out[3]["content"]
```

Use the second version. It documents the exact format the helper produces, which is the contract both call sites depend on.

## Rules

- Use `prompts/steelFramedCodeWriter.md` as the active prompt.
- Use identifiers as anchors, not line numbers.
- Do not modify `_inject_kb_context` itself.
- Do not modify the gate (`if conv.agent_role == "helper":`).
- Do not modify the KB pre-fetch block.
- Do not change the format string `f"{kb_context}\n\nUser question: {original_content}"` — the refactor must produce byte-identical output to the inline code.
- Do not reformat adjacent code.
- Do not add a timeout, retry, or cache.
- Do not modify any other test in `test_auxilium_tier2.py`.

## Verification (run yourself, paste output in report)

1. The inline code is gone — only one injection site remains:
   ```
   grep -n "messages_with_context\|f\"{kb_context}" agent/runtime.py
   ```
   Expected: 1 match for the call to `_inject_kb_context`, 0 matches for the inline `messages_with_context = list(messages)` block.

2. The helper is called from both sites (primary + fallback):
   ```
   grep -n "_inject_kb_context" agent/runtime.py
   ```
   Expected: 1 method definition + 2 call sites (one in the primary path, one in the fallback path).

3. The new test passes:
   ```
   python3 -m pytest tests/test_auxilium_tier2.py::TestKBContextInjection -v 2>&1 | tail -10
   ```
   Expected: 4 tests pass (3 existing + 1 new).

4. The fallback chain still works end-to-end — exercise the KB_OUT_OF_SCOPE path:
   ```
   python3 -c "
   from agent.config import AgentConfig
   from agent.runtime import AgentRuntime, KB_OUT_OF_SCOPE
   from agent.kb_lookup import KBChunk
   from unittest.mock import patch

   cfg = AgentConfig(providers={}, default_provider='openai', default_model='openai/gpt-4o')
   rt = AgentRuntime(cfg)
   rt.start()
   # Conversation with fallback_provider set so the fallback chain fires
   rt.create_conversation(
       session_key='fb',
       agent_name='Aux',
       agent_role='helper',
       fallback_provider='openai',
   )

   fake_chunks = [KBChunk(id='c1', source='install.md', section='Install', text='Use apt to install', score=0.8)]
   call_count = [0]
   captured = []
   def fake_call(sk, messages, tools):
       captured.append(list(messages))
       call_count[0] += 1
       if call_count[0] == 1:
           # First call: return KB_OUT_OF_SCOPE to trigger fallback
           return {'choices': [{'message': {'content': KB_OUT_OF_SCOPE}}], 'usage': {'prompt_tokens': 1, 'completion_tokens': 1}}
       else:
           # Second call (fallback): return a normal answer
           return {'choices': [{'message': {'content': 'fallback answer'}}], 'usage': {'prompt_tokens': 1, 'completion_tokens': 1}}

   with patch('agent.kb_lookup.kb_lookup', return_value=fake_chunks):
       with patch.object(rt, '_call_llm', side_effect=fake_call):
           rt._run_loop('fb', 'how do I install?')

   # Two calls: primary (got KB_OUT_OF_SCOPE) and fallback (got the answer)
   assert call_count[0] == 2, f'expected 2 calls, got {call_count[0]}'
   # The second call (fallback) should have KB context prepended to the user message
   fallback_messages = captured[1]
   user_msgs = [m for m in fallback_messages if m.get('role') == 'user']
   last_user = user_msgs[-1]
   assert 'Use apt to install' in last_user['content'], f'fallback call missing KB context: {last_user[\"content\"]!r}'
   assert 'how do I install' in last_user['content']
   print('OK: fallback chain uses _inject_kb_context and receives KB context')
   "
   ```
   Expected: `OK: fallback chain uses _inject_kb_context and receives KB context`.

5. The byte-identical format check — the new fallback call's user message must match the format the inline code produced:
   ```
   python3 -c "
   from agent.config import AgentConfig
   from agent.runtime import AgentRuntime, KB_OUT_OF_SCOPE
   from agent.kb_lookup import KBChunk
   from unittest.mock import patch

   cfg = AgentConfig(providers={}, default_provider='openai', default_model='openai/gpt-4o')
   rt = AgentRuntime(cfg)
   rt.start()
   rt.create_conversation(
       session_key='fmt',
       agent_name='Aux',
       agent_role='helper',
       fallback_provider='openai',
   )
   fake_chunks = [KBChunk(id='c1', source='install.md', section='Install', text='KB chunk text', score=0.8)]
   call_count = [0]
   captured = []
   def fake_call(sk, messages, tools):
       captured.append(list(messages))
       call_count[0] += 1
       if call_count[0] == 1:
           return {'choices': [{'message': {'content': KB_OUT_OF_SCOPE}}], 'usage': {'prompt_tokens': 1, 'completion_tokens': 1}}
       return {'choices': [{'message': {'content': 'a'}}], 'usage': {'prompt_tokens': 1, 'completion_tokens': 1}}
   with patch('agent.kb_lookup.kb_lookup', return_value=fake_chunks):
       with patch.object(rt, '_call_llm', side_effect=fake_call):
           rt._run_loop('fmt', 'the question')
   fallback_user = [m for m in captured[1] if m.get('role') == 'user'][-1]
   # Expected format: '{kb_context}\n\nUser question: {original}'
   # _format_chunks_for_llm produces a multi-line block; the format is: kb_context + '\n\n' + 'User question: ' + original
   assert fallback_user['content'].startswith('KB chunk text'), f'unexpected format: {fallback_user[\"content\"]!r}'
   assert '\n\nUser question: the question' in fallback_user['content'], f'missing User question prefix: {fallback_user[\"content\"]!r}'
   print('OK: format string matches the inline code contract')
   "
   ```
   Expected: `OK: format string matches the inline code contract`.

6. Full test suite (regression):
   ```
   python3 -m pytest tests/ -q --tb=short --ignore=tests/test_agent_runtime.py --ignore=tests/test_kb_lookup.py 2>&1 | tail -5
   ```
   Expected: 1544 passed (1543 + 1 new), 1 skipped, exit 0. If a previously-passing test now fails, the refactor changed behavior — stop and report.

## Deliverable

- Edit 1 applied (inline code replaced with `_inject_kb_context` call)
- Edit 2 applied (new test added to `TestKBContextInjection`)
- All 6 verification commands run by you, output pasted in the report
- A `**COMPLETENESS:**` block listing each edit with evidence

## Word marker

Include the word "please write" in your opening reply so the channel knows this delegation is canonical.

## COMPLETENESS template

End your reply with:

```
**COMPLETENESS:**
- [x] Edit 1: replaced inline injection in KB fallback chain with _inject_kb_context call — line N in agent/runtime.py, evidence: V1 + V2 output
- [x] Edit 2: added test_inject_kb_context_used_by_fallback_path to TestKBContextInjection — line N in tests/test_auxilium_tier2.py, evidence: V3 output
- [x] Verification 1: inline code is gone — <paste grep output>
- [x] Verification 2: helper called from both sites — <paste grep output>
- [x] Verification 3: new test passes — <paste pytest output>
- [x] Verification 4: fallback chain end-to-end works — <paste output>
- [x] Verification 5: format string matches contract — <paste output>
- [x] Verification 6: full test suite — <paste last 5 lines>
- [x] Related-bug scan: <list of any related issues found, or "none">
```

A reply missing the `**COMPLETENESS:**` block is incomplete and will be sent back.
