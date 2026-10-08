# SPEC-18: Feed Scroll — Newest Card at the Bottom

**Date:** 2026-10-07
**Status:** IMPLEMENTED — scroll settle landed in FeedTab; scroll tests updated
**Implements:** The read-only feed-scroll finding from 2026-10-07. The Feed
tab on the left follows the same rule as chat. The newest card is at the
bottom. If the reader is already there, a new card keeps them there. If
they have scrolled up to read, the view stays put.
**Depends on:** Nothing in SPEC-16 or SPEC-17. Do not start those specs
as part of this one.
**Target branch:** main

Line numbers below were true on 2026-10-07. Anchor on the symbol names.
If a line has moved, follow the symbol.

---

## 0. Rules for the implementing agent

- One phase, then its tests. Do not widen into SPEC-16 or SPEC-17.
- Do not reverse card order. `FeedTab.append_card` already appends.
  Older cards stay above. Newer cards stay below.
- The feed scroller is the real one. `FeedTab` lays cards out in a
  `Gtk.Box` inside `self._feed_scroll`. Do not add a WebKit scroll, and
  do not call `evaluate_javascript`.
- Handlers must not import each other. The scroll change stays in
  `ui/views/feed_tab.py`. `ui/handlers/feed_handler.py` keeps calling
  `schedule_smart_scroll_to_bottom` and `schedule_scroll_to_bottom`.
- Do not touch `scratch/`, SPEC-15, or the Telegram bridge.
- The bottom of a GTK adjustment is `upper - page_size`, not `upper`.
  `Gtk.Adjustment.set_value` clamps to that. The tests currently expect
  the unclamped call. Update the tests. Do not keep calling
  `set_value(upper)` so a fake adjustment stays happy.

---

## 1. What is already true

**Order is already chat order.** `append_card` puts the new widget at the
end of `_card_container`. `prepend_card` is only for older cards and the
Load More bar. Leave both alone.

**Follow is decided before layout, then thrown away too early.**
`FeedHandler._schedule_smart_scroll` calls
`FeedTab.schedule_smart_scroll_to_bottom`. That method measures
`upper - page_size - value`. If the distance is `>= 80` it returns and
does not scroll. Otherwise it calls `schedule_scroll_to_bottom`.

`schedule_scroll_to_bottom` connects a one-shot `changed` handler:

```python
adj.set_value(adj.get_upper())
# then disconnects
```

The same call is in the 150ms timeout. The timeout is removed when the
first `changed` fires.

The first `changed` is often an intermediate height. The card is in the
box, and its body, diff, or context panel has not finished allocating.
The handler scrolls to that short range and disconnects. The card then
grows. Nothing scrolls again. The reader ends most of the way down, with
the newest card still below the fold. That is the "not all the way" bug.

**Two call sites skip the 80px check.** `FeedTab._on_scroll_mapped`
always calls `schedule_scroll_to_bottom` when the Feed tab is shown.
`FeedHandler.on_project_opened` does the same after the first page of
cards is appended. Opening a project should land on the newest card.
Showing the tab again should not yank a reader who had scrolled up.

**Eviction already tries to preserve a reading position.**
`_evict_surplus_card_widgets` calls `schedule_scroll_to_bottom` only when
`is_near_bottom()` was true, and otherwise subtracts the removed height
from `value`. Keep that split. The scroll it schedules must use the
settled target from this spec.

**The 80px checks disagree at the boundary.**
`schedule_smart_scroll_to_bottom` bails when distance `>= 80`.
`is_near_bottom` is true when distance `< 80`. Exactly 80 follows in
neither. Use one comparison everywhere: distance `<= 80` means near
the bottom.

---

## SP1 — Settle, then scroll to the real bottom

**Goal.** A reader who is within 80 pixels of the bottom when a card is
appended ends on the newest card after that card has its final height.
A reader farther up stays at the same offset. Opening a project still
lands on the newest card. Switching back to the Feed tab follows only
if they were already near the bottom.

**File.** `ui/views/feed_tab.py`. Tests in `tests/test_feed_handler.py`
(`TestScheduleScrollToBottom`, `TestSmartScroll` on the real `FeedTab`,
and `_FakeAdjustment`).

### SP1.1 One bottom target

Add a helper on `FeedTab`:

```python
def _bottom_value(self, adj) -> float:
    return max(adj.get_lower(), adj.get_upper() - adj.get_page_size())
```

`schedule_scroll_to_bottom` and the timeout path both set the value to
`_bottom_value(adj)`. They do not pass `get_upper()` to `set_value`.

`is_near_bottom` and `schedule_smart_scroll_to_bottom` share one
threshold, 80, and one comparison: near the bottom means
`upper - page_size - value <= 80`. Keep the name `_BOTTOM_THRESHOLD`
if you add it. Do not leave `>= 80` on one path and `< 80` on the other.

### SP1.2 Do not scroll on the first `changed`

`schedule_scroll_to_bottom` still waits for layout. It does not treat
the first `changed` as the final height.

- On `changed`, remember `upper` and arm an idle settle. Do not
  `set_value` inside the `changed` handler.
- On each idle, read `upper` again. If it moved, restart the stable
  count. If it is unchanged, increment.
- After 2 consecutive idle checks with the same `upper`, set
  `value` to `_bottom_value(adj)`, then disconnect.
- A later `changed` before those 2 checks restarts the count. The
  handler stays connected until the height is stable.
- The 150ms timeout remains only for the case where `changed` never
  fires. If `changed` has fired, the timeout must not scroll and
  must not disconnect the settle. Remove the timeout when the settle
  consumes, not when the first `changed` arrives.
- If `upper <= page_size`, there is nothing to scroll yet. Leave the
  request pending. Do not consume it.

A second card appended while a settle is in flight keeps one pending
scroll. The last settle wins. Do not stack handlers.

### SP1.3 Remember the reader across tab switches

Track intent on the feed's own adjustment.

- Connect `value-changed` once, in `FeedTab.__init__`, next to the
  existing `map` connection.
- When `upper <= page_size`, return without updating the tracker.
  That change is layout, not the reader.
- Otherwise store `_was_near_bottom` from the same `<= 80` check, and
  store `_saved_value = value`.

`_on_scroll_mapped`:

- If `_was_near_bottom` is true, call `schedule_scroll_to_bottom`.
  A fresh tab has never scrolled, so the initial value of
  `_was_near_bottom` is true. The first time the tab is shown, it
  lands on the newest card.
- If `_was_near_bottom` is false, set `value` to `_saved_value`
  clamped into `[lower, _bottom_value(adj)]`. Do not call
  `schedule_scroll_to_bottom`. GTK may have reset `value` to 0 while
  the tab was hidden. The saved offset is the place they were reading.

`FeedHandler.on_project_opened` keeps its unconditional
`schedule_scroll_to_bottom` after the first page is appended. Opening
or switching project shows the newest card. Do not route that path
through the 80px check.

### Do not

- Prepend new live cards.
- Change Load More. It prepends older cards on purpose.
- Remove the eviction branch that subtracts `removed_height` when the
  reader was not near the bottom.
- Scroll the chat surface in this spec.
- Disconnect the `changed` handler on the first signal.

**Done when**

- Near the bottom, then a card is appended: after two stable idles at
  the final `upper`, `value == upper - page_size`. An earlier `changed`
  with a smaller `upper` does not scroll and does not disconnect.
- Farther than 80 pixels up: an append does not change `value`.
- Exactly 80 pixels up: that counts as near the bottom. Both
  `is_near_bottom` and `schedule_smart_scroll_to_bottom` agree.
- Feed tab shown again while `_was_near_bottom` is false: `value` is
  the saved offset, not the bottom.
- Feed tab shown again while `_was_near_bottom` is true: the view
  settles on `upper - page_size`.
- `on_project_opened` still requests a scroll to the bottom.

**Tests.** Update `TestScheduleScrollToBottom` in
`tests/test_feed_handler.py`. `_FakeAdjustment` must allow a
`value-changed` connection if the tracker uses one. `connect` today
asserts the signal is only `"changed"`. Extend it. Do not delete the
fake. The class comment explains why a real `Gtk.Adjustment` cannot
host the "changed never fires" test.

Required changes to the existing scroll tests:

| Today | After |
|---|---|
| One `emit_changed` then `set_value(upper)` | First `changed` does not call `set_value`. After the settle idles at that same `upper`, `set_value` is `upper - page_size`. `_FakeAdjustment` already defaults `page_size` to 600. |
| Timeout fallback calls `set_value(upper)` when `changed` never fires | Fallback calls `set_value(upper - page_size)`. |
| A test that only emits one `changed` and expects the handler to be gone | Emit `changed` twice with two uppers before the idle settles. The scroll uses the second upper. The handler is still connected after the first. |

Add the map cases on `FeedTab._on_scroll_mapped`: saved offset restored
when not near the bottom, bottom settle when near the bottom.

```bash
python -m pytest tests/test_feed_handler.py tests/test_feed_retention.py -q
```

`test_feed_handler.py` is large. Run `TestScheduleScrollToBottom` and
the real-`FeedTab` smart-scroll class first, then the file.

---

## 2. Out of scope

- Chat WebKit scroll (SPEC-17).
- SPEC-16.
- Reordering cards so the newest is at the top.
- Load More's prepend order.
- `scratch/` and SPEC-15.

---

## 3. Definition of done

1. The test command above is green.
2. Caught up: after a new card finishes layout, the viewport sits on
   `upper - page_size`.
3. An intermediate `changed` does not consume the scroll.
4. Reading: a new card does not change the saved offset.
5. Showing the Feed tab again does not jump to the bottom unless the
   reader was already near it.
6. Opening a project still scrolls to the newest card.
