# PHASE 1 of 1 — Deferred Smart Scroll with Proximity Check

**Spec:** `docs/specs/SPEC-SMART-SCROLL-DEFERRED-PROXIMITY.md`

## Files to change

1. `ui/views/feed_tab.py` — Add new method `schedule_smart_scroll_to_bottom()` right after `schedule_scroll_to_bottom()` (around line 287)
2. `ui/handlers/feed_handler.py` — Line ~188: swap `smart_scroll_to_bottom()` → `schedule_smart_scroll_to_bottom()`
3. `tests/test_feed_handler.py` — Add `TestScheduleSmartScrollToBottom` class with 3 tests at end of file

## Rules

- **Use the `steelFramedCodeWriter` prompt at `prompts/steelFramedCodeWriter.md`**
- **READ ALL FILES BEFORE STARTING** — read every file you touch in full before writing any code
- Run: `cd /path/to/projects/crabcakes && python3 -m pytest tests/test_feed_handler.py -q --tb=short` and paste the output
- For any removals: run `grep` and confirm output is 0
- Report: files changed with line numbers, test results, any issues
- At the end, include a COMPLETENESS checklist

## Detailed instructions

### Edit 1: `ui/views/feed_tab.py` — Add `schedule_smart_scroll_to_bottom()`

Add a **new method** right after `schedule_scroll_to_bottom()` (after line ~287, before `smart_scroll_to_bottom()` at line ~289).

The method:
1. Checks `self._feed_scroll` is not None (guard)
2. Gets `vadj` from `self._feed_scroll.get_vadjustment()` (guard)
3. Proximity check: `distance_from_bottom = upper - page_size - current`. If >= 80, return (user scrolled up, don't auto-scroll)
4. If near bottom: call `self.schedule_scroll_to_bottom()` (reuse existing deferred scroll)

Key insight: the proximity check works fine with the stale upper because it's measuring *the user's reading position*, not the future content height.

### Edit 2: `ui/handlers/feed_handler.py` — One-line swap

Find the line inside `_append()` that calls `self._feed_tab.smart_scroll_to_bottom()` and change it to `self._feed_tab.schedule_smart_scroll_to_bottom()`.

### Edit 3: `tests/test_feed_handler.py` — Add `TestScheduleSmartScrollToBottom` class

Add at the end of the file with 3 tests:

1. **`test_schedule_smart_scrolls_when_user_near_bottom`** — vadjustment value is near the bottom → calls through to `schedule_scroll_to_bottom` → verify the `"changed"` handler gets connected and scroll fires.

2. **`test_schedule_smart_does_not_scroll_when_user_scrolled_up`** — vadjustment value is 500px above bottom → verify `schedule_scroll_to_bottom` is NOT called, no handler connected, no timeout installed.

3. **`test_schedule_smart_uses_stale_upper_for_proximity_not_future`** — Set up the adjustment with a known upper, verify that the proximity check uses that upper (not some post-layout value). Pins the design decision.

Use the existing `MockFeedTab` and `MockVadjustment` classes already in the test file. You may need to add a `schedule_smart_scroll_to_bottom` mock to `MockFeedTab` that mirrors the real logic, or test against the real `FeedTab` if GTK is available.

## What NOT to do

- Do NOT modify `smart_scroll_to_bottom()` itself
- Do NOT modify `schedule_scroll_to_bottom()`
- Do NOT modify `scroll_to_bottom()`
- Do NOT add new imports to `feed_tab.py` (reuse existing GLib import pattern)
