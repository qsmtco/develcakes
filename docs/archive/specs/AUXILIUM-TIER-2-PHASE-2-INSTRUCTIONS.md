# Phase T2-2 — KB synthesis in `_run_loop()` for auxilium

**Spec:** `/path/to/projects/crabcakes/docs/specs/SPEC-auxilium-tier-2.md` §2.2
**Target:** main
**Risk:** Medium-High (touches the hot loop — runs on every user message; must not regress the existing KB fallback chain)
**Lines:** +18 (3 changes — KB pre-fetch gate, `_call_llm` call site, new `_inject_kb_context` method)

## Goal

Make `agent/runtime.py:_run_loop()` always run `kb_lookup()` for auxilium (`agent_role == "helper"`) on every user message, and inject the KB context into the primary LLM call. This is **additive** — the existing KB fallback chain (which fires on `KB_OUT_OF_SCOPE`) is unchanged.

## Files to change

1. `agent/runtime.py` — three changes in one file

## Change A: KB pre-fetch gate (in `_run_loop`)

**Anchor:** the `kb_context = None` block in `_run_loop`, currently around lines 1126-1136. The pattern to find:

```python
                # KB pre-fetch: if fallback is configured, pre-fetch KB chunks
                # for potential synthesis when the fallback fires.
                kb_context = None
                if conv.fallback_provider:
                    try:
                        from agent.kb_lookup import kb_lookup
                        chunks = kb_lookup(text, top_k=5, min_score=0.35)
                        if chunks:
                            kb_context = _format_chunks_for_llm(chunks)
                    except Exception:
                        pass  # No KB context — fallback LLM answers without grounding
```

**Replace the gate** `if conv.fallback_provider:` with `if conv.agent_role == "helper":`. Everything else in the block stays identical (the import, the call, the try/except, the assignment to `kb_context`).

**Replace the comment** above the block. The new comment must say this is for auxilium KB synthesis, not the KB fallback chain. The KB fallback chain is a separate code path that fires later (when primary returns `KB_OUT_OF_SCOPE`). Suggested new comment:

```python
                # KB synthesis (Tier 2): for auxilium, run kb_lookup on every
                # user message and inject chunks into the primary LLM call.
                # This is separate from the KB fallback chain (which fires when
                # the primary returns KB_OUT_OF_SCOPE — see lines ~1177-1241).
                kb_context = None
                if conv.agent_role == "helper":
                    try:
                        from agent.kb_lookup import kb_lookup
                        chunks = kb_lookup(text, top_k=5, min_score=0.35)
                        if chunks:
                            kb_context = _format_chunks_for_llm(chunks)
                    except Exception:
                        pass  # kb_lookup is fail-soft — kb_context stays None, LLM proceeds without KB
```

## Change B: `_call_llm` call site (in `_run_loop`)

**Anchor:** the `response = self._call_llm(...)` call, currently at line 1141. Replace the single line with the conditional form:

```python
                # Inject KB context into the primary LLM call for auxilium.
                # If kb_context is None (no relevant chunks or lookup failed), this is a no-op.
                messages_for_call = messages
                if kb_context:
                    messages_for_call = self._inject_kb_context(messages, kb_context, text)
                response = self._call_llm(session_key, messages_for_call, tools)
```

**Important:** do not change `_call_llm`'s signature, the assignment to `response`, or anything after this call. The variable name `messages_for_call` is new — make sure no other code in the function uses the same name (it doesn't, per the spec's verification claim).

## Change C: New method `_inject_kb_context` (on `AgentRuntime`)

**Anchor:** add the method just before `_run_loop`. Find the `# ── AgentRuntime ──` class banner (or wherever the `class AgentRuntime:` lives) and the `def _run_loop` method. The new method must be inside the `AgentRuntime` class (it's a `self` method, called from `_run_loop`).

**Method signature:** `_inject_kb_context(self, messages: list[dict], kb_context: str, text: str) -> list[dict]`

**Method body:** copy from the spec exactly (this is the spec's reference implementation). Verify the function name, parameters, and behavior against the spec at `docs/specs/SPEC-auxilium-tier-2.md` §2.2. The spec's code is authoritative.

Anchor for placement: immediately before `def _run_loop`. The method does:
1. Find the last user message in `messages` (scan from end)
2. If found: build a shallow copy of `messages`, replace the last user message with a new dict that has `f"{kb_context}\n\nUser question: {original_content or text}"` as content
3. If no user message found: return `messages` unchanged (defensive)

**Why shallow copy is safe:** the only mutation is replacing one dict with a new dict at the last-user-message index. All other dicts in `messages` are the same Python objects. `_call_llm` only reads `messages` (per the spec's verification claim at §10 Risks #1).

## Rules

- Use `prompts/steelFramedCodeWriter.md` as the active prompt.
- Use identifiers as anchors, not line numbers.
- Do not reformat adjacent code.
- Do not "improve" comments.
- Do not modify the existing KB fallback chain (lines ~1177-1241) — out of scope.
- Do not modify `_call_llm`, `_format_chunks_for_llm`, or `to_api_messages`.
- Do not add a timeout, retry, or cache to `kb_lookup` — out of scope.
- Do not modify `auxilium.md`, the test file (that's Phase T2-4), or ARCHITECTURE.md (that's Phase T2-6).

## Verification (run yourself, paste output in report)

1. Imports + signature check:
   ```
   python3 -c "
   from agent.runtime import AgentRuntime
   import inspect
   assert hasattr(AgentRuntime, '_inject_kb_context'), 'method missing'
   sig = inspect.signature(AgentRuntime._inject_kb_context)
   params = list(sig.parameters.keys())
   assert params == ['self', 'messages', 'kb_context', 'text'], f'wrong params: {params}'
   print('OK: _inject_kb_context signature correct')
   "
   ```
   Expected: `OK: _inject_kb_context signature correct`. If `method missing`, the placement anchor failed.

2. Behavioral test — `_inject_kb_context` finds last user message and prepends context:
   ```
   python3 -c "
   from agent.config import AgentConfig
   from agent.runtime import AgentRuntime
   cfg = AgentConfig(providers={}, default_provider='openai', default_model='openai/gpt-4o')
   rt = AgentRuntime(cfg)
   messages = [
       {'role': 'system', 'content': 'You are Auxilium.'},
       {'role': 'user', 'content': 'first question'},
       {'role': 'assistant', 'content': 'first answer'},
       {'role': 'user', 'content': 'follow-up question'},
   ]
   out = rt._inject_kb_context(messages, '[KB Context chunks]', 'follow-up question')
   assert out is not messages, 'must return a new list, not mutate'
   assert out[0] is messages[0], 'system message must be the same object'
   assert out[1] is messages[1], 'first user message must be the same object'
   assert out[2] is messages[2], 'first assistant message must be the same object'
   assert out[3] is not messages[3], 'last user message must be a new dict'
   assert '[KB Context chunks]' in out[3]['content']
   assert 'follow-up question' in out[3]['content']
   print('OK: injection prepends to last user message only')
   "
   ```
   Expected: `OK: injection prepends to last user message only`.

3. Defensive test — no user message in list returns unchanged:
   ```
   python3 -c "
   from agent.config import AgentConfig
   from agent.runtime import AgentRuntime
   cfg = AgentConfig(providers={}, default_provider='openai', default_model='openai/gpt-4o')
   rt = AgentRuntime(cfg)
   messages = [{'role': 'system', 'content': 'sys'}, {'role': 'assistant', 'content': 'asst'}]
   out = rt._inject_kb_context(messages, '[KB]', 'text')
   assert out is messages, 'no user message: should return same list'
   print('OK: defensive — no user message returns unchanged')
   "
   ```
   Expected: `OK: defensive — no user message returns unchanged`.

4. Behavioral test — gate change: `_run_loop` calls `kb_lookup` for `agent_role == "helper"` and not for others. (This is the integration test — uses `_run_loop` end-to-end with mocks.)
   ```
   python3 -c "
   from agent.config import AgentConfig
   from agent.runtime import AgentRuntime
   from models.conversation import Conversation
   from unittest.mock import patch, MagicMock

   cfg = AgentConfig(providers={}, default_provider='openai', default_model='openai/gpt-4o')
   rt = AgentRuntime(cfg)
   rt.start()

   # Helper role: kb_lookup IS called
   rt.create_conversation(session_key='h', agent_name='Aux', agent_role='helper', system_prompt='s')
   with patch('agent.runtime.kb_lookup', return_value=[]) as mock_kb:
       with patch.object(rt, '_call_llm') as mock_call:
           mock_call.return_value = {'choices': [{'message': {'content': 'a'}}], 'usage': {'prompt_tokens': 1, 'completion_tokens': 1}}
           rt._run_loop('h', 'how do I configure?')
   assert mock_kb.call_count == 1, f'kb_lookup should fire once, got {mock_kb.call_count}'

   # Non-helper role: kb_lookup NOT called
   rt.create_conversation(session_key='c', agent_name='Coder', agent_role='coder', system_prompt='s')
   with patch('agent.runtime.kb_lookup') as mock_kb:
       with patch.object(rt, '_call_llm') as mock_call:
           mock_call.return_value = {'choices': [{'message': {'content': 'a'}}], 'usage': {'prompt_tokens': 1, 'completion_tokens': 1}}
           rt._run_loop('c', 'how do I configure?')
   assert mock_kb.call_count == 0, f'kb_lookup should not fire, got {mock_kb.call_count}'

   # Empty role: kb_lookup NOT called
   rt.create_conversation(session_key='e', agent_name='Empty', system_prompt='s')
   with patch('agent.runtime.kb_lookup') as mock_kb:
       with patch.object(rt, '_call_llm') as mock_call:
           mock_call.return_value = {'choices': [{'message': {'content': 'a'}}], 'usage': {'prompt_tokens': 1, 'completion_tokens': 1}}
           rt._run_loop('e', 'hi')
   assert mock_kb.call_count == 0, f'empty role: kb_lookup should not fire, got {mock_kb.call_count}'

   print('OK: gate fires for helper, not for non-helper, not for empty')
   "
   ```
   Expected: `OK: gate fires for helper, not for non-helper, not for empty`.

5. Behavioral test — `kb_lookup` returns chunks → `_call_llm` receives prepended context:
   ```
   python3 -c "
   from agent.config import AgentConfig
   from agent.runtime import AgentRuntime, KBChunk
   from models.conversation import Conversation
   from unittest.mock import patch

   cfg = AgentConfig(providers={}, default_provider='openai', default_model='openai/gpt-4o')
   rt = AgentRuntime(cfg)
   rt.start()
   rt.create_conversation(session_key='k', agent_name='Aux', agent_role='helper', system_prompt='s')

   fake_chunks = [KBChunk(text='Gateway config is at ~/.config/crabcakes/', source='configuration.md', section='Gateway', score=0.8)]
   captured_messages = []
   def fake_call(sk, messages, tools):
       captured_messages.extend(messages)
       return {'choices': [{'message': {'content': 'a'}}], 'usage': {'prompt_tokens': 1, 'completion_tokens': 1}}

   with patch('agent.runtime.kb_lookup', return_value=fake_chunks):
       with patch.object(rt, '_call_llm', side_effect=fake_call):
           rt._run_loop('k', 'how do I configure?')

   user_msgs = [m for m in captured_messages if m.get('role') == 'user']
   assert len(user_msgs) >= 1
   last_user = user_msgs[-1]
   assert 'Gateway config' in last_user['content'], f'KB content not injected: {last_user[\"content\"]!r}'
   assert 'how do I configure' in last_user['content']
   print('OK: KB chunks injected into last user message')
   "
   ```
   Expected: `OK: KB chunks injected into last user message`.

6. Behavioral test — multi-turn: every message triggers a fresh `kb_lookup`:
   ```
   python3 -c "
   from agent.config import AgentConfig
   from agent.runtime import AgentRuntime
   from unittest.mock import patch

   cfg = AgentConfig(providers={}, default_provider='openai', default_model='openai/gpt-4o')
   rt = AgentRuntime(cfg)
   rt.start()
   rt.create_conversation(session_key='m', agent_name='Aux', agent_role='helper', system_prompt='s')

   queries = []
   def fake_lookup(question, *, top_k, min_score):
       queries.append(question)
       return []
   def fake_call(sk, messages, tools):
       return {'choices': [{'message': {'content': 'a'}}], 'usage': {'prompt_tokens': 1, 'completion_tokens': 1}}

   with patch('agent.runtime.kb_lookup', side_effect=fake_lookup):
       with patch.object(rt, '_call_llm', side_effect=fake_call):
           rt._run_loop('m', 'first question about Linux')
           rt._run_loop('m', 'follow-up about Windows')

   assert len(queries) == 2, f'expected 2 lookups, got {len(queries)}'
   assert 'Linux' in queries[0]
   assert 'Windows' in queries[1]
   print('OK: every message triggers fresh kb_lookup')
   "
   ```
   Expected: `OK: every message triggers fresh kb_lookup`.

7. Test suite (regression check):
   ```
   python3 -m pytest tests/ -q --tb=short --ignore=tests/test_agent_runtime.py --ignore=tests/test_kb_lookup.py 2>&1 | tail -10
   ```
   Must show no NEW failures.

## Deliverable

- All three changes applied
- All seven verification commands run by you, output pasted in the report
- A `**COMPLETENESS:**` block listing each change with evidence
- A related-bug scan: if you find other gates in `_run_loop` that should also check `agent_role`, or other places that should call `kb_lookup`, flag them — do NOT silently fix.

## Word marker

Include the word "please write" in your opening reply so the channel knows this delegation is canonical.

## COMPLETENESS template

End your reply with:

```
**COMPLETENESS:**
- [x] Change A: KB pre-fetch gate changed from fallback_provider to agent_role == "helper" — line N in agent/runtime.py, evidence: V4 output
- [x] Change B: _call_llm call wrapped with messages_for_call and injection — line N in agent/runtime.py, evidence: V5 output
- [x] Change C: new _inject_kb_context method added — line N in agent/runtime.py, evidence: V1, V2, V3 outputs
- [x] Verification 1: imports + signature — <paste output>
- [x] Verification 2: _inject_kb_context prepends to last user — <paste output>
- [x] Verification 3: defensive — no user message returns unchanged — <paste output>
- [x] Verification 4: gate fires for helper, not for others — <paste output>
- [x] Verification 5: KB chunks injected into last user — <paste output>
- [x] Verification 6: multi-turn — fresh kb_lookup each message — <paste output>
- [x] Verification 7: full test suite — <paste last 10 lines>
- [x] Related-bug scan: <list of any related issues found, or "none">
```

A reply missing the `**COMPLETENESS:**` block is incomplete and will be sent back.
