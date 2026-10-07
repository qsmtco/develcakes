# MICRO-UNIT: Smart Scroll for the HTML Chat Surface

**STATUS: IMPLEMENTED (2026-10-07)** — 1 build round + 1 fix round + 1 re-audit + 1 micro-fix; closes SPEC-06 register #8 WITHOUT the JS bridge (see non-goals).

**Re-audit record (2026-10-07):** v2 fixes verified (N=2 settle, collapse guard, forced-scroll sweep) — PASS with 4 net findings, all closed in the micro-fix: BUG#7 (deferred-follow user-grab race → live-tracker re-read guards in BOTH surfaces; 2 RED-first tests), BUG#10 (report filename drift — ack), BUG#11/BUG#12 (stale comments corrected). 194→201 tests across touched suites; 6+5 kill-proofs sha-verified.

**Fix-round record (Debugger audit → all landed):**
- BUG#1 (HIGH): intermediate `changed` consumed the capture → idle-deferred settle check (`_schedule_restore`/`_settle_restore`; upper must be stable across a frame). Test: `test_intermediate_height_does_not_consume_capture` (burst 0→200→1200, preserve=300).
- BUG#14 (HIGH): tests failed under `DEVELCAKES_NO_WEBKIT=1` → runtime capability gate `hasattr(ChatSurface, "_load_html")` (no env-name hardcode). Verified: 10 skipped / 0 failed.
- ISSUE#6: dead `min()` dropped (GTK set_value clamps); misleading docstring fixed.
- ISSUE#10: destroy test hardened (`handler_is_connected` pins both ids; M8 kill RED).
- INFO#12: REGISTER #8 comment → CLOSED by MICRO-SMART-SCROLL.
- pyright: +41 new findings eliminated via assert-narrowing helper; back to 2 pre-existing baseline.
- Kill-proofs 4/4 RED sha-verified (restore / capture / defer / disconnect).
- Edge-3 ruling (PM, option a): clamps to new max, may snap to bottom — spec updated.

**Build-round record:** RED-first 9/9 (2 vacuous tests hardened pre-audit), battery 96 passed (surface + scroll + welcome), ruff 0 new, pyright 0 (surface) / 2-baseline (tests).

---


**Date:** 2026-10-07 · **Supervisor:** Supervisor · **Builder:** Coder · **Auditor:** Debugger
**Bug source:** PM report (2026-10-07) + SPEC-06 register #8 + chat_surface docstring
"SP-later may add scroll restore if the PM asks" — the PM is asking.
**Read-only verification on file:** full-document `load_html` collapses height mid-
load → vadjustment clamps → snap to top on EVERY append; autoscroll is a registered
no-op on the WebKit surface (main_content.py REGISTER #8).

## Goal

Social-feed scroll: **follow** new messages ONLY when the view is at (or near) the
bottom; **preserve** the reading position when the user has scrolled up.

## Mechanism (in ui/views/chat_surface.py only)

1. Track at-bottom state: connect the surface's OWN vadjustment
   (`self._scroll.get_vadjustment()`) `value-changed` →
   `self._was_at_bottom = (upper - page_size - value) <= 80` (the same 80px
   threshold main_content already uses for the scroll button).
2. In `_do_render`, BEFORE `_load_html`: if the vadjustment is live, capture
   `self._pending_restore = (self._was_at_bottom, current_value)`.
3. Restore AFTER the new document's height lands: connect vadjustment
   `changed` (one-shot, auto-disconnect after fire) →
   - `was_at_bottom=True` → `vadj.set_value(upper - page_size)` (bottom)
   - `was_at_bottom=False` → `vadj.set_value(captured_value)` (clamped to the
     new upper automatically by GTK).
4. On the FIRST render ever (no prior content), land at bottom.

## Edge cases the implementation MUST handle (audit will probe each)

| # | Case | Required behavior |
|---|---|---|
| 1 | Rapid successive messages (coalesced renders) | One pending restore; the LAST capture wins; no double-scroll |
| 2 | Window resize (vadjustment `changed` fires from allocation, not content) | Resize does NOT scroll — distinguish content-height changes (upper changes while at same value? probe) from position changes; if un-distinguishable reliably, document the chosen rule and pin it |
| 3 | Deque window shrink (rows dropped from top → upper shrinks while reading old content) | Position clamps to the new max (no past-bottom, no crash) — **may snap to bottom** (PM ruling 2026-10-07, option a: a dropped window means the old reading position no longer corresponds to content; bottom is the useful landing). Pinned by test_deque_shrink_clamps_gracefully. |
| 4 | First message in a fresh surface | Lands at bottom (no stale restore from empty state) |
| 5 | TextViewFallback | Its autoscroll ALREADY WORKS (pinned by scroll tests) — must NOT regress; same at-bottom tracking acceptable but existing behavior is the contract |
| 6 | User grabs scrollbar mid-load (between capture and restore) | Restore still applies (it restores the pre-load intent); the NEXT user scroll re-arms tracking normally |
| 7 | `destroy()` mid-load | No restore fires on a dead surface; signal disconnected |

## Non-goals

- No JS bridge (register #8's alternative) — this closes the register WITHOUT it.
- No change to main_content's scroll button logic (it keeps working; the surface
  now self-heals first).
- No change to the coalesced-render design (JS-off stays; ruling R2 intact).

## Acceptance criteria

- [ ] At-bottom + append → view follows the new message (stays at bottom)
- [ ] Scrolled-up (reading) + append → reading position preserved (± a few px of
      the captured value once the new height lands)
- [ ] Window resize alone → no scroll change
- [ ] First message → lands at bottom
- [ ] Fallback scroll tests stay green (no regression)
- [ ] `scroll_chat_to_bottom` (button/API) still works
- [ ] Kill-proofs: revert the restore → at-bottom test RED; revert the
      at-bottom capture → preserve test RED; sha-verified
- [ ] Full suite green, ruff 0 new, pyright 0

## Battery

- `xvfb-run pytest tests/test_chat_surface.py tests/test_chat_render_scroll.py -q`
- kill-proof script + sha restores
- Supervisor: full suite at close-out (single-owner rule)
