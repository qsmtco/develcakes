# SPEC-UI-RESPONSIVENESS — Re-audit Fix Instructions (Round 2)

**Spec:** `docs/specs/SPEC-UI-RESPONSIVENESS.md`
**Re-audit:** 7 new bugs (1 CRITICAL, 2 MEDIUM, 4 LOW)

Load steelFramedSpecWriter fresh from `/path/to/projects/crabcakes/prompts/steelFramedSpecWriter.md`. Read the spec in full. Fix all 7 bugs.

---

## NEW BUG #13 (CRITICAL): Fix 1 does not actually defer the prompt build

**Root cause:** `create_conversation` (runtime.py:498) calls `build_system_prompt` synchronously and passes the result to `Conversation(system_prompt=...)`. When `_ensure_system_prompt` runs later in the background thread, `conv.system_prompt` is already non-empty, so it returns immediately (no-op). The 300ms freeze still happens on the main thread.

**Fix — Option 1 (recommended): Add `defer_prompt_build` parameter to `create_conversation`**

Add a new parameter to `create_conversation`:

```python
def create_conversation(
    self,
    ...,
    defer_prompt_build: bool = False,
) -> str:
```

When `defer_prompt_build=True`:
- Skip the `build_system_prompt` call entirely
- Pass `system_prompt=""` to the `Conversation` constructor
- The conversation is registered with an empty system prompt
- `_ensure_system_prompt` (called from `_run_loop` when `_defer_prompt=True`) builds it in the background

The spec's §2.1 must document this parameter change. The §2.2 wiring in `send_to_special_agent` must pass `defer_prompt_build=True` when creating a new conversation.

**Update the spec's code samples in §2.1 and §2.2 to reflect this.** Remove the "in the future" comment (lines 208-212) — this IS the future.

Also remove the contradictory "don't modify create_conversation at all" text (lines 270-272). The spec must commit to Option 1.

---

## NEW BUG #14 (MEDIUM): Stale thread-safety narrative

**Problem:** Lines 80-81 still say "conv.system_prompt assignment is protected by self._lock in _run_loop (line 786)" — this was identified as false in the original audit. The revision added double-checked-locking to `_ensure_system_prompt` but didn't update the narrative.

**Fix:** Replace lines 80-81 with:

```markdown
**Verification of thread safety:**
- `build_system_prompt()` in `agent/context.py` is a pure function — no state, no GTK calls, only file I/O
- `conv.system_prompt` assignment in `_ensure_system_prompt` uses double-checked-locking: fast-path check without lock, slow-path write under `self._lock`. The first check is the common case (prompt already built or deferred-build not enabled).
- Concurrent writers to `conv.system_prompt` exist (`force_llm_compact` from main thread). The double-check inside `self._lock` prevents concurrent overwrites.
```

---

## NEW BUG #15 (MEDIUM): Stale line numbers

**Problem:** Multiple line numbers are wrong (runtime.py shifted from ~3000 to 1995 lines during the extraction phases). Most critical: `_do_error` claimed at ~1294, actual is 1719.

**Fix:** Re-verify ALL line numbers in the spec against the current source. Run these commands and update the spec:

```bash
grep -n "def send_message" agent/runtime.py          # spec says 535, verify
grep -n "def _run_loop" agent/runtime.py              # spec says 782, verify
grep -n "def create_conversation" agent/runtime.py   # verify
grep -n "def _ensure_system_prompt" agent/runtime.py # NEW method
grep -n "def _do_error" ui/handlers/agent_runtime_handler.py  # spec says ~1294, actual 1719
grep -n "def _do_text_delta" ui/handlers/agent_runtime_handler.py
grep -n "def send_to_special_agent" ui/handlers/agent_runtime_handler.py
grep -n "def update_streaming" ui/handlers/chat_render_handler.py
grep -n "_streaming_text" ui/handlers/agent_runtime_handler.py | head -1
grep -n "self._lock" agent/runtime.py | head -1
grep -n "except Exception as e:" agent/runtime.py | tail -1  # _run_loop's outer except
```

Update every line number in the DISCOVERY block (line 18) and §7 edge cases to match.

---

## NEW BUG #16 (LOW): Stale set_markup reference

**Problem:** Line 383 says "throttles internally to 150ms for set_markup" — but Fix 5 removes set_markup from the streaming path.

**Fix:** Change to "throttles internally to 150ms for set_text (the throttle applies to any label update, regardless of whether set_text or set_markup is used)".

---

## NEW BUG #17 (LOW): New test loses XSS coverage

**Problem:** The replacement test `test_update_streaming_shows_plain_text` asserts plain-text behavior but doesn't verify that `end_streaming` → `build_role_bubble` escapes HTML on the final render. The original XSS invariant is lost.

**Fix:** Add a second test to §2.6 that verifies escaping on final render:

```python
def test_end_streaming_escapes_html_in_final_bubble(self):
    """end_streaming → build_role_bubble escapes < > & in the final bubble.
    Streaming shows plain text; escaping is applied on completion."""
    self.handler.start_streaming("agent:1", self.fake_box, "Agent")
    self._run_all_idle()
    self.handler.update_streaming("agent:1", "Use <div> & <script>")
    self._run_all_idle()
    self.handler.end_streaming("agent:1")
    self._run_all_idle()
    # The final bubble should have escaped the HTML chars
    # (verify via the bubble's label content — it should contain &lt; not <)
    final_widget = self.fake_box.get_last_child()
    # Walk the widget tree to find the label and check its content
    # The exact assertion depends on build_role_bubble's structure
```

Note: the exact assertion may need to be adjusted during implementation based on how `build_role_bubble` exposes its label content. The spec should document the intent: "verify escaping happens on final render, not during streaming."

---

## NEW BUG #18 (LOW): Unflagged visual change (cursor space)

**Problem:** Fix 5 changes the cursor from `<tt>▍</tt>` (no space) to `" ▍"` (space before cursor). This is an unflagged visual change.

**Fix:** Add to §2.5: "Note: This changes the cursor rendering from `<tt>▍</tt>` (Pango monospace tag, no leading space) to ` ▍` (plain text with leading space). The space provides visual separation between text and cursor. This is a minor visual change."

---

## NEW BUG #19 (LOW): DCL pitfall — conv reference captured outside lock

**Problem:** In `_ensure_system_prompt`, `conv` is fetched without the lock. Between the fetch and the lock acquisition, another thread could replace the conversation in `self._conversations` (e.g., via `load_conversation` or `clear_conversation`). The slow-path write would then target a stale `conv` object.

**Fix:** Re-fetch `conv` inside the lock and use an identity check:

```python
def _ensure_system_prompt(self, session_key: str) -> None:
    # Fast path: check without lock (common case — prompt already built)
    conv = self._conversations.get(session_key)
    if conv is None or conv.system_prompt:
        return

    # Build the prompt outside the lock (expensive I/O)
    new_prompt = build_system_prompt(...)

    # Slow path: acquire lock, re-fetch conv, identity check, write
    with self._lock:
        conv_now = self._conversations.get(session_key)
        if conv_now is conv and not conv_now.system_prompt:
            conv_now.system_prompt = new_prompt
            logger.info("System prompt built for %s in background thread", session_key)
```

The identity check (`conv_now is conv`) ensures we only write to the SAME object we checked. If the conversation was replaced between the fast-path and slow-path, we skip the write (the new conversation will either have its own prompt or trigger a new build on its next `_run_loop`).

---

## Summary

7 bugs to fix in the spec:
1. BUG #13 (CRITICAL): Add `defer_prompt_build` parameter to `create_conversation`
2. BUG #14 (MEDIUM): Update thread-safety narrative
3. BUG #15 (MEDIUM): Re-verify all line numbers
4. BUG #16 (LOW): Fix stale set_markup reference
5. BUG #17 (LOW): Add XSS test for end_streaming
6. BUG #18 (LOW): Note cursor visual change
7. BUG #19 (LOW): Fix DCL pitfall with identity check

Rewrite the spec. Report what you changed.
