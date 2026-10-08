# AUDIT FIX ROUND — SPEC-17 SP1 (BUG #1) + SPEC-18 SP1 (BUG #2)

**Source:** Debugger adversarial audit (2026-10-07), both bugs reproduced with harnesses
and uncovered by the existing test suites. Fixes are small and local. This is a mid
bug-fix loop — do not restructure anything else.

**Files:** `ui/views/chat_surface.py` + `tests/test_chat_surface.py` (BUG #1);
`ui/views/feed_tab.py` + `tests/test_feed_handler.py` (BUG #2). Nothing else.

---

## BUG #1 (MEDIUM, SPEC-17) — Second load's scroll intent consumed by the first load's FINISHED

**Where:** `ui/views/chat_surface.py:576-598` (`_on_load_changed`), `:560-574`
(`_on_scroll_read`), `:485` (`_do_render`).

**The race (auditor-proven):** `_load_html` can be called while a PRIOR load is still
in flight (WebKit serializes/queues it). The single `_pending_scroll` slot gets
overwritten by the second read → the first load's FINISHED consumes the second's
intent → the second load's FINISHED finds `None` → reader stranded at the TOP of the
newest document. Sequence: append m1 → read 1 done → load 1 → append m2 mid-load-1 →
read 2 done `(False, 300)` → load 2 → FINISHED load 1 (applies 300 to load-1 doc) →
FINISHED load 2 (nothing) → reader at top.

**Fix — approach (b), the one the auditor recommended (matches spec §SP1.2's
"schedule one more render after this scroll is issued"):**

1. Add `self._load_in_flight = False` in `__init__`.
2. Set it **True** in `_load_html` immediately before the real `load_html` call
   (NOT in the monkeypatchable path... careful: `_load_html` IS the monkeypatch seam
   used by tests — set the flag in `_do_render`/`_on_scroll_read` right before calling
   `_load_html`, so the flag semantics survive test monkeypatching of `_load_html`.
   Pick ONE set-site and document it in a comment).
3. **Gate `_do_render`:** if `_load_in_flight` is True → set `_dirty = True` and
   return WITHOUT issuing a read or load. The FINISHED handler's existing dirty-kick
   will re-render after the apply. (No read → no intent overwrite → no queued load.)
4. Clear the flag in `_on_load_changed` when the event is
   `WebKit.LoadEvent.FINISHED` **and** when it is `WebKit.LoadEvent.FAILED`
   (a failed load never reaches FINISHED — without the FAILED clear, one load error
   wedges the surface: every later render sees `_load_in_flight` and defers forever).
   Order: apply intent → kick re-render → clear flag (or clear before the kick — your
   call; document it).
5. `destroy()` resets `_load_in_flight = False`.

**Tests (RED-first — write the failing test from the auditor's sequence BEFORE the fix):**
- `test_second_append_during_load_defers_to_finished_kick`: append m1 → drain (read
  issues) → complete read → load 1 issued, flag True → append m2 → drain `_do_render`
  → assert NO second read, NO second load, `_dirty` True.
- `test_finished_applies_original_intent_then_kicks_fresh_render`: continue the
  sequence → FINISHED load 1 → assert the apply used read-1's intent, load 2 now
  issued (kick), fresh read for load 2, flag re-armed. Final: BOTH loads got an apply.
- `test_load_failed_clears_in_flight_flag`: fire `WebKit.LoadEvent.FAILED` → assert a
  subsequent `_do_render` is NOT blocked (no wedge).
- Do not break the existing `test_read_in_flight_coalesces_to_one_load` — its
  contract (coalesced appends → one load) still holds.

## BUG #2 (MEDIUM, SPEC-18) — Map's restore undone by a stale pending settle

**Where:** `ui/views/feed_tab.py:490-510` (`_on_scroll_mapped`, not-near-bottom branch).

**The race (auditor-proven):** a settle armed earlier (project-open smart scroll,
eviction, or an earlier append) is still pending when the user — now scrolled up —
hides/shows the tab. The map restores `_saved_value`, but the stale settle later fires
and yanks them to the bottom. Auditor harness: `set_value_calls = [120, 120, 150]`
(the 150 is the yank).

**Fix:** at the top of the not-near-bottom branch (before the restore), call
`self._cancel_settle()` and `self._disarm_scroll(vadj)`. (The near-bottom branch is
clean — `schedule_scroll_to_bottom` already cancels prior settles internally.)

**Tests (RED-first):**
- `test_map_cancels_pending_settle_before_restore`: pre-arm (fake
  `_scroll_handler_id=999` + armed `_settle_source`), tracker not-near-bottom,
  `_saved_value=120` → call `_on_scroll_mapped(None)` → assert restore to 120 AND
  `_settle_source is None` AND the changed handler disconnected. Then fire a `changed`
  + pump idles → assert NO further set_value (the yank is gone).

**Ride-along (auditor Issue #4, same file, 10 lines):** add `emit_value_changed()` to
`_FakeAdjustment` (fires handlers whose stored signal is `"value-changed"`) and ONE
test that drives the tracker through it — including the `upper <= page_size` guard
(artifact → tracker unchanged) and a real scroll-up (tracker flips False, saves the
offset). This closes the "tracker logic is never directly tested" gap that let BUG #2
hide.

## Verification (paste ALL outputs)

```bash
xvfb-run -a .venv/bin/python -m pytest tests/test_chat_surface.py tests/test_chat_render_scroll.py tests/test_html_guard_sites.py -q
xvfb-run -a .venv/bin/python -m pytest tests/test_feed_handler.py tests/test_feed_retention.py -q
ruff check ui/views/chat_surface.py ui/views/feed_tab.py tests/test_chat_surface.py tests/test_feed_handler.py --output-format concise | tail -3
pyright ui/views/chat_surface.py ui/views/feed_tab.py 2>&1 | tail -2
```

Bars: chat suite ≥ 126 (122 + new RED-first tests), feed suite ≥ 276, ruff per-file
baselines (chat 0 / main_content 6 / feed_tab 16 — the logging fix from the fix-round
is already in; do not add findings), pyright 0.

## Report format

Files changed + line numbers; RED proof (each new test failing pre-fix, paste output);
all verification outputs; COMPLETENESS checklist (BUG#1 fix sites, BUG#2 fix sites,
each new test); related issues flagged not fixed. Word marker: **please write**.
