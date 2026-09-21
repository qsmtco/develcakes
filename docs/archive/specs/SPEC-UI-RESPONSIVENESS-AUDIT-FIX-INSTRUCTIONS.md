# SPEC-UI-RESPONSIVENESS — Audit Fix Instructions

**Spec:** `docs/specs/SPEC-UI-RESPONSIVENESS.md`
**Audit:** 12 bugs found by Debugger (2 CRITICAL, 4 MEDIUM, 5 LOW, 1 cleared)

Load steelFramedSpecWriter fresh from `/path/to/projects/crabcakes/prompts/steelFramedSpecWriter.md`. Read the spec in full. Fix all 12 bugs below. Output the revised spec.

---

## BUG #1 (CRITICAL): Thread-unsafe conv.system_prompt write

**Problem:** The proposed `_ensure_system_prompt` writes `conv.system_prompt = new_prompt` without holding `self._lock`. The spec claims the lock at `_run_loop` line 786 protects this — it doesn't. That lock block only guards the `conv = self._conversations.get(session_key)` lookup. The `force_llm_compact` method (called from the main thread via `/compact`) reads/writes `conv.system_prompt` concurrently.

**Fix:** Wrap the system_prompt assignment in `with self._lock:`:

```python
def _ensure_system_prompt(self, session_key: str) -> None:
    conv = self._conversations.get(session_key)
    if conv is None:
        return
    if conv.system_prompt:
        return  # Already has one

    # ... build new_prompt (this is pure file I/O, safe outside lock) ...

    with self._lock:
        # Re-check under lock — another thread may have built it concurrently
        if not conv.system_prompt:
            conv.system_prompt = new_prompt
    logger.info(...)
```

The double-check pattern (check outside lock for fast path, re-check inside lock for safety) avoids holding the lock during the expensive `build_system_prompt` call while still preventing the race.

Also add a note in the spec's Risk section: "The `force_llm_compact` method (called from main thread via `/compact`) also writes `conv.system_prompt`. The double-checked-locking pattern above prevents concurrent writes."

---

## BUG #2 (CRITICAL): Fix 5 contradicts acceptance criterion #5

**Problem:** `test_update_streaming_escapes_html_chars` (tests/test_chat_render_handler.py:211) asserts `&lt;div&gt;` in the label markup. Fix 5 changes `set_markup` to `set_text`, so the label will contain literal `<div>`. The spec says "all existing tests pass" (criterion #5) AND "tests may need adjustment" (Step 1) — contradictory.

**Fix:** The spec must be explicit. Add a new section §2.7 "Test Impact":

```markdown
### 2.7 Test Impact of Fix 5 (set_text during streaming)

Fix 5 changes the streaming label from `set_markup` (Pango markup) to
`set_text` (plain text). This breaks tests that assert on escaped markup
content in the streaming bubble:

**Affected test:** `test_update_streaming_escapes_html_chars`
(tests/test_chat_render_handler.py:211)

This test asserts that `&lt;div&gt;` appears in the label after streaming
`<div>hello</div>`. With `set_text`, the label contains the literal text
`<div>hello</div> ▍` — the escaping is no longer applied during streaming.

**Resolution:** Update the test to assert on plain-text behavior during
streaming. The escaping test should move to `end_streaming` / `build_role_bubble`
where the full formatting IS applied:

```python
def test_update_streaming_shows_plain_text(self):
    """During streaming, label shows plain text (no markup escaping).
    Escaping is applied in end_streaming → build_role_bubble."""
    # ... setup streaming bubble ...
    handler.update_streaming("agent:1", "<div>hello</div>")
    label = handler._streaming_bubbles["agent:1"].label.get_label()
    assert "<div>hello</div>" in label  # literal, not escaped
    assert "&lt;" not in label  # no markup escaping during streaming
```

The original escaping test should be re-added as a test on `build_role_bubble`
or `end_streaming` to verify escaping happens on final render.

**Acceptance criterion #5 is amended:** "All existing tests pass EXCEPT
`test_update_streaming_escapes_html_chars`, which is updated to assert
plain-text behavior during streaming."
```

---

## BUG #3 (LOW): Misnamed entry point in §7

**Problem:** Spec says `_handle_streaming_delta` is called from `_on_feed_event` — it's actually called from `on_chat_event`.

**Fix:** In §7 Edge Cases (gateway agent path), change `_on_feed_event` to `on_chat_event`.

---

## BUG #4 (MEDIUM): Redundant §7 mitigation

**Problem:** §7 Edge Case "Conversation creation on first send" proposes adding `_on_agent_start_cb(session_key)` at the start of `_run_loop` for immediate activity indicator. But BUG #21 already dispatches an empty `_on_text_delta` at line 805-806 as a turn-start signal — which already triggers the activity indicator via `_do_text_delta` → `_on_agent_start_cb`. The mitigation is redundant.

**Fix:** Remove the proposed mitigation. Replace with: "The BUG #21 turn-start signal (empty `_on_text_delta` dispatch at line 805) fires after `_ensure_system_prompt` returns, providing the activity indicator. No additional mitigation needed."

---

## BUG #5 (LOW): Missing edge case for error during streaming

**Problem:** Spec doesn't address what happens to the streaming bubble if `_on_error` fires mid-stream (e.g., LLM timeout after partial streaming).

**Fix:** Add to §7: "If `_on_error` fires during streaming (e.g., TimeoutError after partial text), `_do_error` calls `end_streaming(session_key)` which finalizes the bubble with whatever text was accumulated. The final bubble uses `build_role_bubble` with the partial text + error formatting."

---

## BUG #6 (LOW): Missing visual edge case for 50ms freeze

**Problem:** With the 50ms outer throttle (Fix 3), a burst of 10 tokens arriving within 50ms renders only the first one immediately; the rest are coalesced into the next throttle window.

**Fix:** Add to §7: "Token bursts (10+ tokens within 50ms): only the first token in the burst triggers `update_streaming`. The remaining tokens' text is accumulated in `_streaming_text` and rendered on the next throttle pass (within 50ms). Visually, the bubble updates in small chunks rather than per-token — standard for chat UIs."

---

## BUG #9 (MEDIUM): Incomplete snippet in §2.2

**Problem:** §2.2's code sample for `send_to_special_agent` shows the `defer_prompt = True` path for new conversations but says "// ... existing sync logic for api_key, model, etc." without showing the full `else` branch.

**Fix:** Include the full `else` branch (the existing api_key/model/MCP syncing code) so the implementer doesn't have to guess where the `defer_prompt = False` assignment goes.

---

## BUG #10 (LOW): Partial concurrency audit

**Problem:** The Risk section mentions `cancel()` but not `force_llm_compact` (which is called from the main thread via `/compact` and reads/writes `conv.system_prompt`).

**Fix:** Add to the Risk section: "`force_llm_compact` (called from main thread via `/compact`) reads and potentially rewrites `conv.system_prompt`. The double-checked-locking pattern in `_ensure_system_prompt` (BUG #1 fix) prevents concurrent writes."

---

## BUG #11 (LOW): import time in function body

**Problem:** Fix 3's code sample has `import time` inside `_do_text_delta` instead of at module level.

**Fix:** Move `import time` to the top of the file (it's likely already imported alongside `import logging`). Update the code sample.

---

## BUG #12 (MEDIUM): Missing latency analysis

**Problem:** §3.2 claims "<50ms/sec of main-thread work" and "dropped frames should be eliminated" but doesn't analyze worst-case per-token latency. With nested throttles (50ms outer + 150ms inner), a single token can be delayed up to 200ms before appearing in the bubble.

**Fix:** Add to §3.2 a worst-case latency analysis:

```markdown
**Worst-case per-token latency:** With the outer 50ms throttle (Fix 3) and
the inner 150ms throttle (update_streaming), a token can be delayed up to
200ms before the bubble visually updates. In practice, the inner 150ms
throttle dominates (it's the longer interval), so worst-case is ~150ms
for a token that arrives just after a throttle window opens. This matches
the PRE-FIX behavior (the 150ms throttle already existed). Fix 3's 50ms
outer throttle does NOT increase latency — it reduces throughput to
update_streaming, which the inner throttle was already doing.

**Net effect on user-perceived latency:** unchanged from pre-fix (still
~150ms worst-case for visual update). The improvement is in main-thread
CPU usage (less work per token), not in visual latency.
```

---

## Summary of changes to the spec

1. Add `with self._lock:` double-check in `_ensure_system_prompt` (BUG #1)
2. Add §2.7 Test Impact section, amend acceptance criterion #5 (BUG #2)
3. Fix `_on_feed_event` → `on_chat_event` (BUG #3)
4. Remove redundant §7 mitigation, reference BUG #21 (BUG #4)
5. Add error-during-streaming edge case (BUG #5)
6. Add token-burst visual edge case (BUG #6)
7. Include full else-branch snippet in §2.2 (BUG #9)
8. Add force_llm_compact to Risk section (BUG #10)
9. Move `import time` to top-level (BUG #11)
10. Add worst-case latency analysis to §3.2 (BUG #12)

Rewrite the spec file at `docs/specs/SPEC-UI-RESPONSIVENESS.md` with all fixes applied. Report what you changed.
