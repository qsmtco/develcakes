# Phase 4D — Bug-A/B Audit Follow-up Instructions

**Date:** 2026-06-21
**Supervisor:** Qaster
**Builder:** QTR
**Spec:** `docs/specs/SPEC-FEED-CARD-UX.md` (Phase 4 section, lines 485-560)
**Related post-mortem:** `docs/post-mortems/2026-06-18-FEED-CARD-UX-POST-MORTEM.md`
**Audit report:** `docs/post-mortems/2026-06-21-FEED-CARD-UX-PHASE-4D-AUDIT.md` (written by supervisor)
**Word marker:** "please write"

---

## Context

The 5-phase Feed Card UX implementation shipped on 2026-06-18 (commit `5727675`). During manual testing of Phase 4 (smart scroll), two bugs surfaced in the project-open path:

**Bug A — Scroll-to-top flake on project reopen.** The unconditional `scroll_to_bottom()` in `on_project_opened` reads `vadjustment.upper` synchronously after `append_card()` calls, before GTK has run the layout pass that updates `upper`. Sometimes the stale `upper` equals 0 or a partial value, and `set_value(stale_upper)` lands the feed on top instead of bottom.

**Bug B — `Gtk-WARNING: Broken accounting of active state for widget … (GtkBox)`** fires on project close, when card widgets are unparented while their CSS `PRELIGHT`/`ACTIVE`/`SELECTED` state flags are still set.

Both bugs were initially "fixed" incorrectly (failed fixes documented in the audit report). The correct fixes are in the working tree on top of `c1cad1d` (latest commit) but have **gaps that this phase addresses**:

1. The new `schedule_scroll_to_bottom()` mechanism (vadjustment `changed` signal + 150ms timeout fallback) has **zero test coverage**. The mock in `MockFeedTab` calls `scroll_to_bottom()` directly and bypasses the new logic entirely.
2. The new `_clear_widget_state_recursive()` has **zero test coverage**. The recursive walker is unverified.
3. The timeout fallback in `schedule_scroll_to_bottom` has a cleanup race: if the `changed` signal fires and succeeds, the timeout is not disarmed. If the success path's `disconnect()` raises (e.g., adjustment already disposed during teardown), the timeout fires 150ms later and **silently re-scrolls the feed even if the user has scrolled away**.

This phase fixes the test-coverage gap and the cleanup race. The mechanism choice (vadjustment `changed` signal) is already correct and stays.

---

## Scope (3 phases, 1 file for code, 1 file for tests)

### Phase 4D-1: Add unit tests for `schedule_scroll_to_bottom`

**File:** `tests/test_feed_handler.py` (or a new `tests/test_feed_tab.py` if cleaner)

The `MockFeedTab.schedule_scroll_to_bottom` currently is:
```python
def schedule_scroll_to_bottom(self):
    self.scroll_to_bottom()
```
This bypasses the real mechanism. Replace it with a **real GTK adjustment** (or a duck-typed fake that supports `connect("changed", ...)` + `emit("changed")` + `get_upper()` + `set_value()`) so the new logic is exercised.

**Required test cases (minimum 4):**

1. `test_schedule_scroll_does_not_scroll_immediately_when_upper_is_stale` — Build a fake `Gtk.Adjustment` whose `get_upper()` returns 0 (stale) at connect time, then 1000 after `emit("changed")` is fired. Call `schedule_scroll_to_bottom()`. Assert that `set_value` was NOT called synchronously, and that `set_value(1000)` WAS called after `emit("changed")` fires. This is the regression test for the original Bug A — the fix must wait for `changed`.

2. `test_schedule_scroll_fires_via_timeout_fallback_when_changed_never_fires` — Same setup, but never call `emit("changed")`. After ~200ms (longer than the 150ms timeout), assert that `set_value` WAS called with the current `upper`. This proves the safety net works.

3. `test_schedule_scroll_disconnects_changed_handler_after_fire` — Call `schedule_scroll_to_bottom()`, emit `changed`, then emit `changed` a second time. Assert that `set_value` was called exactly once (the second emit should not trigger a second scroll). Verifies the one-shot disconnect.

4. `test_schedule_scroll_disarms_timeout_after_changed_fires` — Call `schedule_scroll_to_bottom()`, emit `changed` synchronously, then wait 200ms. Assert that `set_value` was called exactly once. The success path must disarm the timeout. **This is the test for the cleanup race in item 3 of the scope.**

5. (Optional but recommended) `test_schedule_scroll_handles_disconnect_exception` — Make the `disconnect` method raise. Emit `changed`. Assert that the timeout fallback does not throw and does not double-scroll. Verifies defensive cleanup.

**Approach:** Build a `_FakeAdjustment` class with `connect(signal, cb) -> int`, `disconnect(handler_id)`, `emit(signal)`, `get_upper() -> float`, `set_value(v) -> None`, `get_value() -> float`. Pass it as `vadj` to `feed_tab._feed_scroll.get_vadjustment()` (or set it via `scroll.set_vadjustment(adj)` if available; otherwise monkeypatch `get_vadjustment`).

You may need to construct a real `Gtk.ScrolledWindow` inside the test to wire the fake adjustment, or use a fake `Gtk.ScrolledWindow` whose `get_vadjustment()` returns the fake. Real GTK widgets in tests are OK if they are created and torn down properly (use `Gtk.ScrolledWindow()` in a test fixture, don't add it to a window).

**Run:** `python3 -m pytest tests/test_feed_handler.py -k "schedule_scroll" -v 2>&1 | tee /tmp/phase4d-1.log`

### Phase 4D-2: Add unit tests for `_clear_widget_state_recursive`

**File:** `tests/test_feed_handler.py` (or a new `tests/test_feed_tab.py`)

The `_clear_widget_state_recursive(widget)` method walks a widget tree and calls `widget.unset_state_flags(PRELIGHT | ACTIVE | SELECTED)` on each node. Required:

1. `test_clear_widget_state_visits_self_and_all_descendants` — Build a real `Gtk.Box` with 2 nested `Gtk.Button` children (one of which has its own `Gtk.Label` child). Set `PRELIGHT` flag on the outer box and on each child. Call `_clear_widget_state_recursive(box)`. Assert that the `PRELIGHT` flag is cleared on all 4 widgets (box + 2 buttons + 1 label).

2. `test_clear_widget_state_handles_widget_without_state_safely` — Call on a fresh `Gtk.Box` that has never had any state flags set. Assert no exception is raised.

3. `test_clear_widget_state_handles_unset_exception_gracefully` — If the underlying `unset_state_flags` raises (e.g., test it with a fake widget whose method raises), assert that the recursion continues to siblings and does not propagate the exception. **This documents the `try/except` behavior in the existing code.**

**Approach:** Use real `Gtk.Box` + `Gtk.Button` + `Gtk.Label` widgets. They are cheap to create and don't need to be shown. `get_state_flags()` and `unset_state_flags()` work on unrealized widgets in GTK4.

**Run:** `python3 -m pytest tests/test_feed_handler.py -k "clear_widget_state" -v 2>&1 | tee /tmp/phase4d-2.log`

### Phase 4D-3: Fix the timeout-fallback cleanup race

**File:** `ui/views/feed_tab.py`, method `schedule_scroll_to_bottom()`

**Current code (lines ~210-260):**
```python
self._scroll_handler_id = vadj.connect("changed", _on_adj_changed)
GLib.timeout_add(150, _timeout_fallback)
```

The success path of `_on_adj_changed` disconnects the signal but does NOT disarm the timeout. The timeout fires 150ms later, sees `_scroll_handler_id is not None` is now False (because the handler cleared it), and returns `SOURCE_REMOVE` without scrolling. **That works in the happy path.**

The buggy path is: success path's `disconnect()` raises (e.g., the adjustment was already disposed by GTK during widget teardown — happens in `on_project_closed`). Then `_scroll_handler_id is not None` stays True, the timeout fires 150ms later, sees `is not None` and scrolls unconditionally. The user may have already scrolled away.

**Fix:** Pass the timeout source ID into the success handler and disarm it there.

```python
def schedule_scroll_to_bottom(self) -> None:
    if self._feed_scroll is None:
        return
    vadj = self._feed_scroll.get_vadjustment()
    if vadj is None:
        return

    # Disconnect any prior one-shot handler
    if self._scroll_handler_id is not None:
        try:
            vadj.disconnect(self._scroll_handler_id)
        except Exception:
            pass
        self._scroll_handler_id = None

    # Also cancel any prior timeout (defensive — re-entrancy guard)
    if self._scroll_timeout_id is not None:
        try:
            GLib.source_remove(self._scroll_timeout_id)
        except Exception:
            pass
        self._scroll_timeout_id = None

    def _on_adj_changed(adj):
        adj.set_value(adj.get_upper())
        # Disarm the timeout — success path wins
        if self._scroll_timeout_id is not None:
            try:
                GLib.source_remove(self._scroll_timeout_id)
            except Exception:
                pass
            self._scroll_timeout_id = None
        # Disconnect ourselves
        if self._scroll_handler_id is not None:
            try:
                adj.disconnect(self._scroll_handler_id)
            except Exception:
                pass
            self._scroll_handler_id = None
        return False

    self._scroll_handler_id = vadj.connect("changed", _on_adj_changed)

    def _timeout_fallback():
        # If we got here, 'changed' didn't fire in time. Scroll now.
        if self._scroll_handler_id is not None:
            vadj.set_value(vadj.get_upper())
            try:
                vadj.disconnect(self._scroll_handler_id)
            except Exception:
                pass
            self._scroll_handler_id = None
        self._scroll_timeout_id = None
        return GLib.SOURCE_REMOVE

    self._scroll_timeout_id = GLib.timeout_add(150, _timeout_fallback)
```

**Also add the instance variable:**
```python
# In __init__ (alongside _scroll_handler_id)
self._scroll_timeout_id: int | None = None
```

**Why this is the right fix:** The success path and the timeout path both clear the other's state. Whichever fires first wins. The leftover state (`_scroll_timeout_id` or `_scroll_handler_id`) is always cleaned up by the surviving path.

**Run:** `python3 -m pytest tests/test_feed_handler.py -k "schedule_scroll" -v 2>&1 | tee /tmp/phase4d-3.log`
(Tests from Phase 4D-1 should all pass; new test `test_schedule_scroll_disarms_timeout_after_changed_fires` is the regression test.)

---

## Rules (use the steelFramedCodeWriter prompt)

Read `/path/to/projects/crabcakes/prompts/steelFramedCodeWriter.md` in full before writing any code. Follow the **Discovery → Hard Part First → Verify → Test → Spec Compliance → Completeness** flow.

**Read these files in full before starting:**

1. `ui/views/feed_tab.py` (the file you are editing for 4D-3)
2. `tests/test_feed_handler.py` (the file you are editing for 4D-1 and 4D-2)
3. `docs/specs/SPEC-FEED-CARD-UX.md` lines 485-560 (Phase 4 section)
4. `docs/post-mortems/2026-06-21-FEED-CARD-UX-PHASE-4D-AUDIT.md` (the audit report — for context on what to fix)
5. `prompts/steelFramedCodeWriter.md` (your standing orders)

---

## Deliverable expectations

For each phase (4D-1, 4D-2, 4D-3), include in your response:

1. **Discovery block** (Rule 1): what files you read and what you learned
2. **Data flow trace** (Rule 0.5): how the new code interacts with existing code
3. **Code changes** with line numbers (paste the relevant `git diff` for the changed region)
4. **Verification commands and outputs** (paste the test output verbatim)
5. **Related-bug scan** (Step 6.6): any other issues you noticed but did NOT fix in this phase, flagged as "Related issue found, not fixed in this phase"
6. **Implementation-choice rationale** (Step 6.7): for any non-obvious design choice
7. **COMPLETENESS checklist** (Step 6.5) with `[x]` or `[NOT DONE]` per item, with evidence (line numbers, grep output, test results)

**Required marker phrase in your reply:** "please write" — confirms the message is canonical and the supervisor's channel recognizes it.

---

## Verification (run before reporting back)

```bash
# Phase 4D-1 + 4D-2 + 4D-3 targeted test runs
python3 -m pytest tests/test_feed_handler.py -k "schedule_scroll" -v
python3 -m pytest tests/test_feed_handler.py -k "clear_widget_state" -v

# Full feed-related suite (regression check)
python3 -m pytest tests/test_feed_handler.py tests/test_feed_card.py tests/test_feed_store.py tests/test_review_handler_feed_card.py

# Syntax check on edited files
python3 -c "import ast; ast.parse(open('ui/views/feed_tab.py').read()); ast.parse(open('tests/test_feed_handler.py').read()); print('OK: both files parse')"

# Verify the timeout-race fix (the test from 4D-1 that specifically targets the new disarm logic)
# This test MUST pass before reporting phase 4D-3 complete.

# Confirm the audit-fix count matches the failure-mode count
# - Bug A: needs at least 1 test (4D-1 #1 above)
# - Bug B: needs at least 1 test (4D-2 #1 above)
# - Cleanup race: needs at least 1 test (4D-1 #4 above)
```

---

## Anti-Patterns to Avoid

- **Do NOT silently fix the timeout race during 4D-1 or 4D-2.** The fix belongs in 4D-3, where the test for it lives. If you notice the race while writing 4D-1, flag it as "Related issue found, not fixed in this phase" — the supervisor will confirm whether 4D-3 should proceed.
- **Do NOT use `MockFeedTab` style mocks for the new `schedule_scroll_to_bottom` tests.** The whole point of 4D-1 is to test the real mechanism. Use a fake `Gtk.Adjustment` that supports `connect` / `disconnect` / `emit` / `get_upper` / `set_value`.
- **Do NOT modify `scroll_to_bottom()` or `smart_scroll_to_bottom()`.** They are working correctly per the 5-phase post-mortem. Only edit `schedule_scroll_to_bottom()` and the new `_scroll_timeout_id` field.
- **Do NOT touch `feed_handler.py` in this phase.** The fix is in `feed_tab.py` only.
- **Do NOT commit at the end.** The supervisor owns the commit/push per `implementationLoop.md` §3.1.
- **Do NOT use `try/except: pass` in the new test code.** Tests should fail loudly on unexpected errors; only catch the specific exception the production code is documented to raise.

---

## Out of Scope (do not fix in this phase)

1. Manual reproduction of Bug A or Bug B in a live GTK runtime. Requires running the crabcakes app and observing user-facing behavior. The unit tests added in 4D-1 and 4D-2 are the automation equivalent; the live test remains a manual follow-up for the captain.
2. The `try/except Exception: pass` in `_clear_widget_state_recursive` (defensive but possibly too broad). Flag as "Related issue found" if you see it during 4D-2 — supervisor decides.
3. Other `try/except` blocks in `feed_tab.py` (e.g., in `replace_card`, `remove_card` for the new `unset_state_flags` call). Same — flag if noticed.
4. Updating `ARCHITECTURE.md` to document the new `schedule_scroll_to_bottom` method. The 5-phase post-mortem's "Next Steps" already lists this; deferred to Tier 2.

---

**End of instructions. Begin with Discovery. Report back with the COMPLETENESS checklist. please write.**
