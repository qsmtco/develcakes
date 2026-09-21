# Phase F1a — Extract `_prepare_kb_synthesis` helper (extract KB block, preserve per-turn cache)

**Source:** Auxilium Tier 2 post-mortem §9 (evolution suggestions: items 1 + 5) + adversarial audit follow-up
**Goal:** Item 1 (extract helper to shrink `_run_loop`) — item 5 (per-turn kb_lookup) is **already implemented** via the `_kb_cache_for_turn` guard
**Risk:** Medium (touches a hot loop and a path the fallback chain depends on)
**Lines:** -20 to -25 in `agent/runtime.py`, +30 to +40 (the new helper), +1-2 tests

## Status: SPEC REVISION (2026-06-17)

The original spec was based on stale assumptions about the current state of `_run_loop`. Pre-flight audit (Qaster, 2026-06-17 22:53 PDT) found:

- **Item 5 is already done.** `kb_lookup` is gated by a per-turn cache variable `_kb_cache_for_turn` that is initialized to `None` outside the `while` loop and only fetched on the first iteration (`if _kb_cache_for_turn is None`). See `agent/runtime.py:1255-1281` (current) vs. the spec's "lines 1161-1183" (stale anchor).
- **Item 1 is still valid.** The KB-synthesis block is ~17 lines and lives inside the `while` loop. Extracting it to a helper shrinks `_run_loop` and improves readability.

The refactor must **preserve the per-turn cache** — replacing the cached code with the spec's "Edit 2" template would be a regression (re-introducing wasteful per-iteration `kb_lookup` calls).

## Current state of the KB block (verified 2026-06-17)

In `agent/runtime.py:_run_loop()`:

```python
            # Per-turn cache: KB chunks fetched once and reused for the entire
            # multi-iteration loop. The user question is the same throughout;
            # re-running kb_lookup on every iteration is wasted work and tokens.
            _kb_cache_for_turn: str | None = None
            _is_helper = (
                isinstance(conv.agent_role, str)
                and conv.agent_role.strip().lower() == "helper"
            )

            while iteration < max_iter:
                ...
                # KB synthesis (Tier 2): for auxilium, run kb_lookup on every
                # user message and inject chunks into the primary LLM call.
                # This is separate from the KB fallback chain (which fires when
                # the primary returns KB_OUT_OF_SCOPE — see lines ~1177-1241).
                # Cached per-turn: same query across iterations of a single user turn.
                kb_context = None
                if _is_helper:
                    if _kb_cache_for_turn is None:
                        try:
                            from agent.kb_lookup import kb_lookup
                            chunks = kb_lookup(text, top_k=5, min_score=0.35)
                            if chunks:
                                _kb_cache_for_turn = _format_chunks_for_llm(chunks)
                        except Exception:
                            pass  # kb_lookup is fail-soft
                    kb_context = _kb_cache_for_turn

                # Inject KB context into the primary LLM call for auxilium.
                # If kb_context is None (no relevant chunks or lookup failed), this is a no-op.
                messages_for_call = messages
                if kb_context:
                    messages_for_call = self._inject_kb_context(messages, kb_context, text)
                response = self._call_llm(session_key, messages_for_call, tools)
```

**The `_kb_cache_for_turn` and `_is_helper` variables live in `_run_loop`'s scope**, initialized OUTSIDE the `while` loop. The cache survives across iterations.

## Goal (revised)

1. Extract the KB pre-fetch + injection prep into a `_prepare_kb_synthesis` helper method on `AgentRuntime`
2. **Preserve the per-turn cache** — the helper takes a `kb_cache` argument and mutates it (or returns the new cache value)
3. Move the helper call OUTSIDE the `while` tool loop, or call it inside the loop but with the cache passed in
4. The fallback chain at line ~1330 still uses `_inject_kb_context` (which takes `messages` and `kb_context` separately) — should not change
5. Add 1 test that verifies `kb_lookup` is called once per `_run_loop` invocation (not once per tool-loop iteration) — **this test will fail on the current code if not careful, because the cache is already in place. The test verifies behavior, not implementation details.**

## Files to change

1. `agent/runtime.py` — add `_prepare_kb_synthesis` method, refactor `_run_loop` to call it
2. `tests/test_auxilium_tier2.py` — add a test for once-per-message behavior (multi-iteration case)

## Edit 1: Add `_prepare_kb_synthesis` method on `AgentRuntime`

**Anchor:** add the new method just before `_run_loop` (around line 1183, immediately after `_compute_model_max`). The method should be a `self` method on `AgentRuntime`.

**Method signature:** `_prepare_kb_synthesis(self, conv, text: str, messages: list[dict], kb_cache: str | None) -> tuple[list[dict], str | None, str | None]`

The 3-tuple return: `(messages_for_call, kb_context, new_cache)`. The caller passes `kb_cache` (current value) and uses `new_cache` to update its own cache variable.

**Method body (template):**

```python
    def _prepare_kb_synthesis(
        self,
        conv: "Conversation",
        text: str,
        messages: list[dict],
        kb_cache: str | None,
    ) -> tuple[list[dict], str | None, str | None]:
        """Prepare KB-synthesis messages for the primary LLM call (Tier 2).

        If conv.agent_role == "helper", runs kb_lookup on the current user
        message (or reuses the cached result) and injects the chunks into
        the messages list. Returns (messages_for_call, kb_context, new_cache).
        For non-auxilium agents or empty KB results, returns
        (messages, None, None) — no injection, no change to the messages.

        The per-turn cache is the caller's responsibility. Pass the current
        cache value in kb_cache; assign the returned new_cache back to the
        caller's variable. This keeps the cache in _run_loop's scope so
        it survives across tool-loop iterations.

        Called once per tool-loop iteration, but kb_lookup itself only
        runs once per _run_loop invocation (gated by the cache).
        """
        # Gate: only fire for auxilium (type-safe, case-insensitive)
        is_helper = (
            isinstance(conv.agent_role, str)
            and conv.agent_role.strip().lower() == "helper"
        )
        if not is_helper:
            return messages, None, None

        # Per-turn cache: only fetch on first call within a turn
        new_cache = kb_cache
        if new_cache is None:
            try:
                from agent.kb_lookup import kb_lookup
                chunks = kb_lookup(text, top_k=5, min_score=0.35)
                if chunks:
                    new_cache = _format_chunks_for_llm(chunks)
            except Exception:
                pass  # kb_lookup is fail-soft

        kb_context = new_cache
        messages_for_call = messages
        if kb_context:
            messages_for_call = self._inject_kb_context(messages, kb_context, text)
        return messages_for_call, kb_context, new_cache
```

**Notes:**

- The gate pattern is the same as the current code (T2-F3 fix).
- The cache logic is preserved exactly — `kb_lookup` only runs when `kb_cache is None`.
- The return tuple is `(messages_for_call, kb_context, new_cache)`. The caller destructures all three and assigns `new_cache` back to its own `_kb_cache_for_turn` variable.
- `_format_chunks_for_llm` and `_inject_kb_context` are already in scope (module-level / method on `AgentRuntime`).

## Edit 2: Refactor `_run_loop` to call the helper

**Anchor:** the KB block in `_run_loop` at lines 1264-1282 (current). Find this pattern:

```python
                # KB synthesis (Tier 2): for auxilium, run kb_lookup on every
                # user message and inject chunks into the primary LLM call.
                # This is separate from the KB fallback chain (which fires when
                # the primary returns KB_OUT_OF_SCOPE — see lines ~1177-1241).
                # Cached per-turn: same query across iterations of a single user turn.
                kb_context = None
                if _is_helper:
                    if _kb_cache_for_turn is None:
                        try:
                            from agent.kb_lookup import kb_lookup
                            chunks = kb_lookup(text, top_k=5, min_score=0.35)
                            if chunks:
                                _kb_cache_for_turn = _format_chunks_for_llm(chunks)
                        except Exception:
                            pass  # kb_lookup is fail-soft
                    kb_context = _kb_cache_for_turn

                # Inject KB context into the primary LLM call for auxilium.
                # If kb_context is None (no relevant chunks or lookup failed), this is a no-op.
                messages_for_call = messages
                if kb_context:
                    messages_for_call = self._inject_kb_context(messages, kb_context, text)
                response = self._call_llm(session_key, messages_for_call, tools)
```

**Replace with:**

```python
                # KB synthesis (Tier 2): prepare messages with KB context if applicable.
                # The helper is called once per tool-loop iteration, but kb_lookup itself
                # only runs once per _run_loop invocation (gated by the per-turn cache
                # passed in via kb_cache). The cache survives across iterations.
                messages_for_call, kb_context, _kb_cache_for_turn = self._prepare_kb_synthesis(
                    conv, text, messages, _kb_cache_for_turn
                )
                response = self._call_llm(session_key, messages_for_call, tools)
```

That's a 19-line → 6-line block (with the comment).

**The fallback chain (line ~1330) is unchanged.** It still uses `_inject_kb_context(messages, kb_context, text)` and the `kb_context` variable from `_run_loop`'s scope. Since `kb_context` is now set by `_prepare_kb_synthesis` inside the loop, it's still available in the outer scope.

**Note on `_is_helper`:** The variable `_is_helper` was a local in `_run_loop` computed before the loop. After this refactor, the helper computes `is_helper` internally. The `_is_helper` variable in `_run_loop` is no longer used and can be **removed** (or left for future use — pick one and document the choice).

**Decision:** Remove `_is_helper` from `_run_loop` (it's dead code after the refactor).

## Edit 3: Add a test for once-per-message behavior (multi-iteration)

**Anchor:** add a new test to `TestKBLookupFiresForAuxilium` in `tests/test_auxilium_tier2.py`.

```python
    def test_kb_lookup_called_once_per_run_loop_invocation(self):
        """kb_lookup should run once per _run_loop call, NOT once per
        tool-loop iteration. The helper is called inside the while loop,
        but the per-turn cache prevents repeated kb_lookup calls.
        """
        rt, sk = _make_runtime(agent_role="helper")

        call_count = [0]
        llm_call_count = [0]

        def counting_lookup(question, *, top_k, min_score):
            call_count[0] += 1
            return []  # empty chunks so we don't add anything

        def fake_call(sk, messages, tools):
            llm_call_count[0] += 1
            if llm_call_count[0] == 1:
                # First call: trigger the tool loop with a tool_calls response
                return {
                    "choices": [{"message": {
                        "content": "",
                        "tool_calls": [{"id": "t1", "type": "function",
                                        "function": {"name": "read_file", "arguments": "{}"}}],
                    }}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                }
            # Second call: normal answer
            return {
                "choices": [{"message": {"content": "answer"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }

        with patch("agent.kb_lookup.kb_lookup", side_effect=counting_lookup):
            with patch.object(rt, "_call_llm", side_effect=fake_call):
                rt._run_loop(sk, "how do I configure?")

        # _call_llm was called twice (tool loop fired), but kb_lookup was called only once
        assert llm_call_count[0] >= 2, f"expected >= 2 LLM calls, got {llm_call_count[0]}"
        assert call_count[0] == 1, f"expected 1 kb_lookup call, got {call_count[0]}"
```

**Note:** This test should PASS on the **current code** (the per-turn cache is already in place). The test is a **regression guard** — if a future refactor removes the cache, the test will fail. The test is added now to lock in the behavior we want to preserve.

## Rules

- Use `prompts/steelFramedCodeWriter.md` as the active prompt.
- Use identifiers as anchors, not line numbers.
- The new helper must be added BEFORE `_run_loop` in the file. Python doesn't strictly require this for methods (methods are resolved at call time), but the convention is definition order.
- Do NOT change the fallback chain at line ~1330. It still uses `_inject_kb_context(messages, kb_context, text)` with the `kb_context` variable from `_run_loop`'s scope.
- Do NOT change the `_inject_kb_context` method itself.
- Do NOT change the gate pattern (`isinstance(conv.agent_role, str) and ...strip().lower() == "helper"`). Use the same pattern in the helper.
- The new helper's return value MUST be a 3-tuple `(messages_for_call, kb_context, new_cache)`. The order matters — `_run_loop` destructures in that order.
- The `_kb_cache_for_turn` variable in `_run_loop` must remain accessible inside the `while` loop (it is, because Python's lexical scope handles this). The helper's third return value is assigned back to it on every iteration.
- **PRESERVE the per-turn cache.** Do not replace the cached code with uncached code. The cache is the optimization that makes the spec's item 5 already-done.
- Remove the now-dead `_is_helper` variable from `_run_loop` (it's no longer used after the refactor).

## Verification (run yourself, paste output in report)

1. The new helper exists:
   ```
   grep -n "def _prepare_kb_synthesis" agent/runtime.py
   ```
   Expected: 1 match.

2. The old per-iteration KB block is replaced with a helper call:
   ```
   grep -n "kb_context = None\|if _is_helper\|if _kb_cache_for_turn is None:" agent/runtime.py
   ```
   Expected: 0 matches inside `_run_loop` (the cache and kb_lookup logic is now inside the helper).

3. `_run_loop` uses the helper:
   ```
   grep -n "_prepare_kb_synthesis" agent/runtime.py
   ```
   Expected: 2 matches (the method definition + the call site in `_run_loop`).

4. The `_is_helper` variable is removed from `_run_loop`:
   ```
   grep -n "_is_helper" agent/runtime.py
   ```
   Expected: 0 matches (or only inside the helper, not in `_run_loop`).

5. The new test passes:
   ```
   python3 -m pytest tests/test_auxilium_tier2.py::TestKBLookupFiresForAuxilium -v 2>&1 | tail -10
   ```
   Expected: 5 tests pass (4 existing + 1 new).

6. End-to-end: tool loop triggers, but `kb_lookup` is called only once:
   ```
   python3 -c "
   from tests.test_auxilium_tier2 import _make_runtime
   from unittest.mock import patch
   rt, sk = _make_runtime(agent_role='helper')
   kb_calls = [0]
   def counting_lookup(question, *, top_k, min_score):
       kb_calls[0] += 1
       return []
   llm_calls = [0]
   def fake_call(sk, messages, tools):
       llm_calls[0] += 1
       if llm_calls[0] == 1:
           return {'choices': [{'message': {'content': '', 'tool_calls': [{'id': 't1', 'type': 'function', 'function': {'name': 'read_file', 'arguments': '{}'}}]}}], 'usage': {'prompt_tokens': 1, 'completion_tokens': 1}}
       return {'choices': [{'message': {'content': 'a'}}], 'usage': {'prompt_tokens': 1, 'completion_tokens': 1}}
   with patch('agent.kb_lookup.kb_lookup', side_effect=counting_lookup):
       with patch.object(rt, '_call_llm', side_effect=fake_call):
           rt._run_loop(sk, 'how do I configure?')
   print(f'LLM calls: {llm_calls[0]}, KB calls: {kb_calls[0]}')
   assert llm_calls[0] >= 2, f'expected >= 2 LLM calls, got {llm_calls[0]}'
   assert kb_calls[0] == 1, f'expected 1 KB call, got {kb_calls[0]}'
   print('OK: tool loop fired but kb_lookup ran once')
   "
   ```
   Expected: `OK: tool loop fired but kb_lookup ran once`.

7. Full test suite for auxilium tier 2:
   ```
   python3 -m pytest tests/test_auxilium_tier2.py -q --tb=short 2>&1 | tail -5
   ```
   Expected: 30 passed (29 existing + 1 new), 0 failed.

## Deliverable

- Edit 1 applied (new helper method preserves the per-turn cache)
- Edit 2 applied (refactored `_run_loop` to call the helper, removed `_is_helper`)
- Edit 3 applied (new test for once-per-message behavior)
- All 7 verification commands run by you, output pasted in the report
- A `**COMPLETENESS:**` block listing each edit with evidence

## Word marker

Include the word "please write" in your opening reply so the channel knows this delegation is canonical.

## COMPLETENESS template

```
**COMPLETENESS:**
- [x] Edit 1: added _prepare_kb_synthesis method (preserves per-turn cache) — line N in agent/runtime.py, evidence: V1 output
- [x] Edit 2: refactored _run_loop to call the helper, removed _is_helper — line N in agent/runtime.py, evidence: V2 + V3 + V4 output
- [x] Edit 3: added test_kb_lookup_called_once_per_run_loop_invocation — line N in tests/test_auxilium_tier2.py, evidence: V5 output
- [x] Verification 1: helper exists — <paste output>
- [x] Verification 2: old per-iteration block is replaced — <paste output>
- [x] Verification 3: _run_loop uses the helper — <paste output>
- [x] Verification 4: _is_helper removed from _run_loop — <paste output>
- [x] Verification 5: new test passes — <paste pytest output>
- [x] Verification 6: end-to-end tool loop fires but KB runs once — <paste output>
- [x] Verification 7: full auxilium tier 2 test suite — <paste last 5 lines>
- [x] Related-bug scan: <list of any related issues found, or "none">
```
