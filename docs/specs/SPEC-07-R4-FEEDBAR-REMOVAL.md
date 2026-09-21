# SPEC-07: R4 — Feedbar Removal

**Date:** 2026-09-20
**Author:** Supervisor (develcakes v2)
**Status:** Draft — for implementation
**Implements:** docs/proposals/DEVELCAKES-V2-CHANGE-LIST.md §5 R4
**Depends on:** SPEC-06 (activity state needs the HTML chat surface to land in)
**Target branch:** main

> Architecture compliance: activity state surfaces in the HTML chat surface; no code
> writes to a dead widget.

---

## 1. Overview

**Problem.** `ui/views/feedbar.py` is the Response Status bar (`set_status_text` →
`set_markup`, `set_progress_fraction`, `set_progress_hidden`). The 6-state activity
machine (`ui/handlers/activity_handler.py`) takes feedbar as a constructor dependency
(`__init__(self, feedbar, main_content, GLib_module=None)` — verified) and calls
`_update_feedbar()` throughout. R4 deletes the widget; its state must land somewhere
first — the HTML chat surface's activity pill (SPEC-06).

**Solution.**
1. Verify the pill covers all 6 activity states + live counters (elapsed bucket,
   streaming label).
2. Rebind `ActivityHandler`'s render half to the surface pill (a `_update_status()`
   target swap — the state machine itself is untouched).
3. Delete feedbar.py + window wiring + feedbar ctor arg.

**Scope**

| In | Out |
|---|---|
| ActivityHandler render rebind | State-machine logic changes (6 states unchanged) |
| feedbar.py deletion | 250 ms ticker removal (it drives the pill now) |
| window.py unwiring | |
| Tests | |

## 2. Changes by File

### ui/handlers/activity_handler.py

Ctor becomes `__init__(self, status_target, main_content, GLib_module=None)` where
`status_target` implements `set_status_text(str)`, `set_progress_fraction(float)`,
`set_progress_hidden(bool)` — the same three-method duck-type feedbar satisfied
(verified: feedbar.py's public surface). The HTML pill adapter implements exactly those
three methods (text→pill text+class; fraction→pill progress; hidden→pill visibility).
All `_update_feedbar()` call sites renamed `_update_status()` (mechanical).

### ui/views/chat_surface.py (from SPEC-06)

Add the adapter (if not already present):

```python
class ActivityPillAdapter:
    def set_status_text(self, text: str) -> None: ...
    def set_progress_fraction(self, f: float) -> None: ...
    def set_progress_hidden(self, hidden: bool) -> None: ...
```

### ui/window.py

Remove `from ui.views.feedbar import FeedBar` (line 37), `self._response_status = FeedBar()`
(line 311), `feedbar=` arg (line 315), `right_box.append(self._response_status)`
(line 853). ActivityHandler constructed with the pill adapter from the active chat
surface (lazy surfaces: handler holds a callable returning the active surface's adapter).

### DELETE

ui/views/feedbar.py; tests/test_feedbar.py (if present — verify; else
test_activity_handler.py feedbar cases repointed to a fake pill).

## 3. Data Flow

Tool start/end/error, streaming, elapsed ticker → ActivityHandler state machine →
`_update_status()` → pill adapter → HTML chat surface DOM (class/text swap).

## 4. File Change Summary

| File | Change | ~Lines | Risk |
|---|---|---|---|
| ui/handlers/activity_handler.py | render rebind + rename | ~40 edits | med |
| ui/views/chat_surface.py | pill adapter (3 methods) | +40 | low |
| ui/window.py | unwire feedbar | −12 | low |
| ui/views/feedbar.py | delete | −7,182 bytes | low |
| tests | repoint | ~80 edits | — |

## 5. Implementation Order

1. Pill adapter + fake-pill tests for ActivityHandler (all 6 states + counters).
2. window.py rewire; feedbar unwired but still present (suite green both ways).
3. Delete feedbar.py + its test; grep `FeedBar\|feedbar` → zero source matches.
4. Full suite + ruff + pyright.

## 6. Acceptance Criteria

- [ ] All 6 activity states + streaming label + elapsed counter visible in the chat pill
- [ ] No code writes to a dead widget; `grep feedbar` zero in source
- [ ] Ticker (250 ms) drives the pill — no orphan GLib sources (source_remove guards)
- [ ] Full pytest green, ruff clean, pyright clean

## 7. Edge Cases

| Case | Behavior |
|---|---|
| No chat surface open (agent tab closed) | Handler no-ops render (state still tracked); pill catches up on next open |
| Multiple surfaces open | Active tab's pill renders; others stale-until-focused (matches current per-tab behavior) |
| Rapid state flapping | Pill updates coalesced via the existing 200 ms throttle note (activity_handler.py:351-355) |

## 8. ARCHITECTURE.md Updates

§Modules/Chat surface — pill marked the activity surface; feedbar removed from map.
