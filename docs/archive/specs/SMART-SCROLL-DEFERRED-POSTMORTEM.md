# Post-Mortem: Deferred Smart Scroll with Proximity Check

**Date:** 2026-06-21
**Phase:** 1 of 1 (single-phase execution)
**Status:** ✅ Complete

## What Changed

### `ui/views/feed_tab.py` (+30 lines)
Added `schedule_smart_scroll_to_bottom()` — a new method that combines:
1. **Proximity check** using stale (pre-layout) `vadjustment.upper` — correct because we're measuring the user's current reading position, not the future content height
2. **Deferred scroll** via `schedule_scroll_to_bottom()` — waits for the `vadjustment` `changed` signal so the scroll target reflects post-layout content height

This fixes the stale-upper bug: when `append_card` + `smart_scroll` ran in the same GTK idle callback, `vadjustment.upper` hadn't been updated yet, causing the scroll target to be wrong.

### `ui/handlers/feed_handler.py` (1 line)
Line 188: `smart_scroll_to_bottom()` → `schedule_smart_scroll_to_bottom()`

### `tests/test_feed_handler.py` (+194 lines)
- Added `schedule_smart_scroll_to_bottom` to `MockFeedTab` mirror
- Updated existing `test_add_card_uses_smart_scroll_not_unconditional` to track new method name
- Added `TestScheduleSmartScrollToBottom` class with 3 tests:
  1. Near-bottom user → delegates → scrolls after layout
  2. Scrolled-up user → no delegation → no scroll
  3. Stale upper used for proximity (design decision pinned)

### `docs/ARCHITECTURE.md` (+7 lines)
Added `schedule_scroll_to_bottom()` and `schedule_smart_scroll_to_bottom()` to FeedTab public API listing.

## What Was NOT Modified (Constraint Verification)

| Method | Status |
|--------|--------|
| `smart_scroll_to_bottom()` | ✅ Unchanged (still synchronous) |
| `schedule_scroll_to_bottom()` | ✅ Unchanged |
| `scroll_to_bottom()` | ✅ Unchanged |

## Test Results

```
51 passed in 0.68s
```

## Adversarial Audit Summary

### Bugs Found

| # | Severity | Description | Status |
|---|----------|-------------|--------|
| 1 | MEDIUM | `schedule_smart_scroll_to_bottom()` missing from ARCHITECTURE.md | ✅ Fixed |
| 2 | LOW | `schedule_scroll_to_bottom()` also missing from ARCHITECTURE.md (pre-existing) | ✅ Fixed |

### Edge Cases Verified

| Case | Behavior | Correct? |
|------|----------|----------|
| `upper < page_size` (content fits in viewport) | distance = negative → scrolls | ✅ Yes — user should see content |
| User scrolled past end | distance = negative → scrolls | ✅ Yes — re-anchor to bottom |
| Exactly 80px from bottom | `>= 80` → does NOT scroll | ✅ Yes — boundary consistent with `smart_scroll_to_bottom` |
| `_feed_scroll is None` | Early return | ✅ Yes — guard clause |
| `vadj is None` | Early return | ✅ Yes — guard clause |

### Related-Bug Scan

- `smart_scroll_to_bottom()` has zero external callers after the swap
- `schedule_scroll_to_bottom()` still correctly used at line 457 for project-open path
- No other files reference the old call pattern

## What Went Well

1. **Spec-before-phase rule worked** — having `SPEC-SMART-SCROLL-DEFERRED-PROXIMITY.md` and `SMART-SCROLL-DEFERRED-PHASE-1-INSTRUCTIONS.md` ready before delegation meant QTR had zero ambiguity
2. **steelFramedCodeWriter prompt** — QTR produced clean, well-documented code on the first try
3. **Adversarial audit caught the docs gap** — the code was correct but ARCHITECTURE.md was stale
4. **Single-phase scope was tight** — one method + one call-site swap + three tests = auditable

## What Could Be Better

1. **ARCHITECTURE.md update should have been in the phase instructions** — the constraint "update ARCHITECTURE.md if public API changes" was known but not included in the delegation payload. QTR followed the instructions literally and skipped it because it wasn't explicitly listed.
2. **Mock mirror could drift** — `MockFeedTab.schedule_smart_scroll_to_bottom` duplicates the proximity logic from the real method. If the real method changes, the mock silently diverges. Consider using the real method in tests (which the new tests do via `real_feed_tab` fixture — good).

## Files Changed

```
docs/ARCHITECTURE.md          |   7 ++++++-
docs/specs/SMART-SCROLL-DEFERRED-PHASE-1-INSTRUCTIONS.md | 3221 bytes (new)
docs/specs/SMART-SCROLL-DEFERRED-POSTMORTEM.md | (this file)
docs/specs/SPEC-SMART-SCROLL-DEFERRED-PROXIMITY.md | 2062 bytes (new)
tests/test_feed_handler.py    | 194 ++++++++++++++++++++++++++++++++++++++++++--
ui/handlers/feed_handler.py   |   2 +-
ui/views/feed_tab.py          |  30 +++++++
```
