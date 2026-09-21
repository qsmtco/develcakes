# Phase 4E — Streaming Token Count Always Zero (Field-Name Mismatch) Instructions

**Date:** 2026-06-21
**Supervisor:** Qaster
**Builder:** QTR
**Spec:** `docs/specs/SPEC-FEED-CARD-UX.md` (Phase 4E section, this file)
**Related post-mortem:** (none yet — will be written at the end of this phase)
**Diagnosis report:** written by supervisor in chat, dated 2026-06-21 10:25 PDT
**Word marker:** "please write"

---

## Context

The FeedBar's "streaming" state shows three live counters: `tokens`, `tok/s`, and elapsed time. The elapsed time works correctly; the token count **always displays 0** and the velocity is consequently always 0 tok/s.

### Root cause (single-line bug)

`ui/handlers/activity_handler.py:469` reads the chat-delta text from the wrong field:

```python
elif event == "chat":
    state = payload.get("state", "")
    if state == "delta":
        self.on_chat_delta(payload.get("text", "") or "", session_key)   # ← BUG
```

The gateway sends chat event payloads in this shape (verified by `tests/test_chat_handler.py:318-323` and `tests/test_missing_message_fix.py:394`):

```python
{"state": "delta", "sessionKey": "...", "message": {"content": "..."}}      # string form
{"state": "delta", "sessionKey": "...", "message": {"content": [{"type": "text", "text": "..."}]}}  # list-of-blocks form
```

The text lives at `payload["message"]["content"]`, not `payload["text"]`. There is no top-level `text` key. `payload.get("text", "")` always returns the default `""`. Downstream, `on_chat_delta` does `count = len(delta_text) if delta_text else 0` → `count = 0` → `_streaming_token_count` is never incremented.

### Why the chat bubble still works

`ui/handlers/chat_handler.py:551-557` reads the correct field (`payload["message"]`) and passes it through `_extract_text` (`chat_handler.py:645-680`), which handles both string and list-of-blocks forms. The activity handler has no such helper and reads the wrong field name.

### Why velocity is also 0

`velocity = token_est / elapsed` (`activity_handler.py:607`). When `token_est = 0` (because `_streaming_token_count = 0`), `velocity = 0`. Velocity is a derived value — it will fix itself once tokens count correctly.

---

## Scope (1 file for code, 1 file for tests, no other changes)

### Phase 4E-1: Add `_extract_chat_text` helper to `ActivityHandler` and use it in the chat-delta branch

**File:** `ui/handlers/activity_handler.py`

**Change A — add helper method (place it immediately after `on_chat_final` at line 123 and before `set_on_assistant_buffer` at line 127 — i.e., at the end of the chat-event method block, so it lives next to the related chat methods):**

Add a new method `_extract_chat_text(payload: dict) -> str` that mirrors `chat_handler._extract_text` (`ui/handlers/chat_handler.py:645-680`):

```python
def _extract_chat_text(self, payload: dict) -> str:
    """Extract plain text from a gateway chat event payload.

    The gateway sends chat event payloads with the text at payload.message.content,
    in one of two forms:
    - A string (simple text responses)
    - A list of typed blocks (block-level formatting: code, quote, media, etc.)

    This helper normalizes both forms into a single string for token counting.
    It is a local copy of chat_handler._extract_text (ui/handlers/chat_handler.py:645)
    to keep handlers decoupled — see tests/conftest.py::test_handlers_do_not_import_each_other
    for the rule. If a third handler ever needs the same logic, promote to
    a shared module (out of scope for this phase).
    """
    msg_obj = payload.get("message", {})
    if isinstance(msg_obj, dict):
        content = msg_obj.get("content", "")
    else:
        content = msg_obj
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type", "")
            if block_type == "text":
                t = block.get("text", "")
                if t:
                    parts.append(t)
            elif block_type == "input_image":
                # Gateway media attachment — not text, but include as a marker
                # so the token count reflects the response's size in spirit.
                # The image content itself is not counted (we cannot estimate
                # its token cost from a URL); we count a 0-length placeholder
                # by simply not appending. Token velocity is dominated by text.
                continue
        return "".join(parts)
    elif isinstance(content, str):
        return content
    return str(content) if content else ""
```

**Change B — fix the dispatcher (line 469):**

Replace
```python
self.on_chat_delta(payload.get("text", "") or "", session_key)
```
with
```python
self.on_chat_delta(self._extract_chat_text(payload) or "", session_key)
```

**Why `or ""` stays:** the helper returns either a string or `str(content)` (the last fallback). It will not return `None`. The `or ""` is defensive belt-and-suspenders and does no harm. It also normalizes the case where `_extract_chat_text` somehow returns an empty list (`""` is falsy, so the `or ""` is a no-op there too). Keep it.

**No other changes** — do not touch `on_chat_delta` itself, do not touch `_streaming_label`, do not touch the velocity calculation, do not refactor `_extract_text` in `chat_handler.py`. The fix is exactly two edits: add the helper, change one line in the dispatcher.

---

### Phase 4E-2: Add unit tests for `_extract_chat_text` and the chat-delta token-count path

**File:** `tests/test_activity_bubbles.py` (add to the existing test class — do not create a new file)

The existing file has `class TestActivityHandlerActivityBubbles` (at line 74) that constructs an `ActivityHandler` with a `MagicMock` feedbar + main_content and calls `on_gateway_event(...)` with various payloads. **Append the new tests to the END of that class** (do not create a new class) so the file remains organized by behavior area. Use the same fixture pattern: `handler = ActivityHandler(feedbar=MagicMock(), main_content=MagicMock(), GLib_module=fake_glib)`. The 7 new test cases to add:

**Required test cases (minimum 5):**

1. `test_chat_delta_increments_token_count_from_string_content` — Build an `ActivityHandler` with a `MagicMock` feedbar and a `fake_glib` fixture. Fire `on_gateway_event("chat", {"state": "delta", "sessionKey": "agent:test:1", "message": {"content": "Hello, world!"}})`. Assert `handler._streaming_token_count == 13` (length of "Hello, world!"). **This is the regression test for the bug — the original buggy code would have asserted 0.**

2. `test_chat_delta_increments_token_count_across_multiple_deltas` — Fire three deltas with DIFFERENT pieces of text (e.g., "Hello", " world", "!"). Assert `_streaming_token_count == 5 + 6 + 1 = 12` (sum of lengths, since each delta `+=` the length of its own text). **Design note (document in a comment in the test):** Per `models/streaming.py:28`, the gateway "sends cumulative text" — so in production, three cumulative deltas would be "Hello", "Hello world", "Hello world!", and the counter would over-count (5 + 11 + 12 = 28 from a 12-character final string). This is a pre-existing design quirk in `on_chat_delta` (uses `+=` not `=`). It is **out of scope** for this phase — the field is misnamed, the accumulation logic is suspect, and either or both should be addressed in a Tier 3 follow-up. For this test, use three DISTINCT delta texts to keep the assertion deterministic and focused on the field-name fix. The Tier 3 fix (cumulative-vs-delta semantics) is documented in the "Out of scope" section below.

3. `test_chat_delta_handles_list_of_blocks_form` — Fire `on_gateway_event("chat", {"state": "delta", "sessionKey": "agent:test:1", "message": {"content": [{"type": "text", "text": "abc"}, {"type": "text", "text": "def"}]}})`. Assert `_streaming_token_count == 6` (length of "abcdef"). This proves the helper handles the list-of-blocks form.

4. `test_chat_delta_handles_input_image_block` — Fire `on_gateway_event("chat", {"state": "delta", "sessionKey": "agent:test:1", "message": {"content": [{"type": "input_image", "image_url": "https://example.com/foo.png"}, {"type": "text", "text": "look at this"}]}})`. Assert `_streaming_token_count == 12` (length of "look at this"). The image block contributes 0 to the count — this is the documented behavior.

5. `test_chat_delta_handles_missing_message_field` — Fire `on_gateway_event("chat", {"state": "delta", "sessionKey": "agent:test:1"})` (no `message` key at all). Assert `handler._streaming_token_count == 0` and no exception is raised. The helper should safely return `""` (the `payload.get("message", {})` default is `{}`, and `_extract_chat_text({})` returns `""` because `content = {}.get("content", "") = ""` and `isinstance("", str)` is True, returning `""`).

6. `test_chat_delta_handles_string_message_field` — Some hypothetical gateway variant might send the message as a string directly (`{"state": "delta", "message": "hello"}`). Fire that. Assert `_streaming_token_count == 5`. The helper's `else: content = msg_obj` branch handles this.

7. `test_extract_chat_text_returns_empty_for_empty_payload` — Direct unit test on the helper: call `handler._extract_chat_text({})`. Assert result is `""`. This pins the helper's contract.

**Approach:** Mirror the existing test style in `tests/test_activity_bubbles.py`. The `fake_glib` fixture is already in `tests/conftest.py:76-99` and provides a no-delay GLib replacement. Use it for `ActivityHandler(GLib_module=fake_glib)`. The feedbar and main_content can be `MagicMock()` — they are not exercised by the chat-delta path.

**Run:** `python3 -m pytest tests/test_activity_bubbles.py -k "chat_delta or extract_chat" -v 2>&1 | tee /tmp/phase4e-2.log`

---

### Phase 4E-3: Verify the fix end-to-end with a manual scenario check (read-only, no code)

This is a verification step, not a code change. The supervisor will run it after the builder reports done.

**What to verify (read-only):**
1. Open crabcakes, start a chat with an agent, send a message.
2. Watch the FeedBar during the response.
3. Confirm the token counter ticks (e.g., starts at 0, climbs to 12, 25, 50, ...).
4. Confirm the velocity counter shows a non-zero value once the response has been streaming for > 0.1s.
5. Confirm the elapsed time counter still works (no regression).

If the token counter still shows 0 after the fix, the bug is elsewhere — re-investigate.

**Run:** `python3 -m pytest tests/test_activity_bubbles.py -k chat_delta -v` (must show all 7 tests passing)
**Run:** `python3 -m pytest tests/test_chat_handler.py -v` (must show 0 regressions — existing chat-handler tests must still pass)

---

## Out of scope (deferred to Tier 3)

1. **The `_streaming_token_count` field is misnamed.** It stores character deltas (via `len(delta_text)`), then divides by 4 in `_streaming_label` as a rough char-to-token approximation. This is a pre-existing design quirk. A real fix would either (a) use the LLM's actual `usage.completion_tokens` from the `chat.final` event, or (b) rename the field to `_streaming_char_count` and document the /4 approximation. Either fix is out of scope for Phase 4E — we just want the field to accumulate something.
2. **The cumulative-vs-delta accumulation.** Per `models/streaming.py:28`, "gateway sends cumulative text" — but `on_chat_delta` does `+= count` (not `=`). This means a cumulative-text gateway would double-count. Need to verify the actual gateway behavior. If cumulative, the fix should be `self._streaming_token_count = count` (replace, not add). Defer verification + fix to a follow-up phase.
3. **The duplicated `_extract_chat_text` between activity_handler and chat_handler.** A future refactor could move both to a shared `ui/handlers/_chat_payload.py` module. Not now.
4. **The token-velocity time-window.** The current `velocity = token_est / elapsed` is a session-lifetime average, not a recent-window velocity. After 60s of streaming, velocity will look low even if recent throughput is high. Future improvement.

---

## Acceptance criteria

1. All 7 new tests pass (`tests/test_activity_bubbles.py -k "chat_delta or extract_chat"`).
2. All existing tests pass (`tests/test_chat_handler.py -v`, `tests/test_activity_bubbles.py -v`).
3. The dispatcher at `activity_handler.py:469` reads from `payload["message"]["content"]` (via the new helper), not from `payload["text"]`.
4. The new helper `_extract_chat_text` is a method of `ActivityHandler` (not a free function, not imported from chat_handler).
5. No other code in `activity_handler.py` is touched (`on_chat_delta`, `_streaming_label`, `_set_state`, the velocity calculation — all unchanged).
6. No file other than `ui/handlers/activity_handler.py` and `tests/test_activity_bubbles.py` is modified.

---

## Commit & post-mortem (handled by supervisor)

The supervisor will:
1. Audit the diff against `steelFramedCodeWriter.md` (builder's playbook).
2. Run `adversarialDebugger.md` against the changes.
3. Mutation-style verify the regression test: revert the line-469 fix, re-run test 1 (`test_chat_delta_increments_token_count_from_string_content`), confirm it fails with `Expected 13, got 0` (or similar), restore the fix, confirm it passes.
4. Write the post-mortem at `docs/post-mortems/2026-06-21-FEED-CARD-UX-PHASE-4E-POST-MORTEM.md` using the §6 format from `implementationLoop.md`.
5. Commit with message: `fix(activity): Phase 4E — read chat-delta text from payload.message.content (token count always zero)`.
6. Push to origin.

The builder does NOT commit, push, or write the post-mortem.

---

## Reference: code locations

- Bug site: `ui/handlers/activity_handler.py:469` (the wrong field read)
- Counter accumulator: `ui/handlers/activity_handler.py:108-117` (`on_chat_delta`)
- Counter display: `ui/handlers/activity_handler.py:600-616` (`_streaming_label`)
- Counter reset: `ui/handlers/activity_handler.py:42, 81` (`__init__`, `on_agent_start`)
- Correct field read (reference): `ui/handlers/chat_handler.py:551-557` (`on_chat_event` dispatcher)
- Reference helper to mirror: `ui/handlers/chat_handler.py:645-680` (`_extract_text`)
- Test fixture (payload shape): `tests/test_chat_handler.py:318-323` (`make_final_payload`)
- List-of-blocks test: `tests/test_missing_message_fix.py:394`
- Conftest GLib fake: `tests/conftest.py:76-99` (`fake_glib` fixture)
- Architecture doc (chat event shape): `docs/ARCHITECTURE.md:2585-2590, 3263-3264` (describes the event but does not document the message.content field path explicitly)
