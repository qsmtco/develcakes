# SPEC-03: P11 Memory-Ratchet Fix — Bounded Feed Surfaces

**Date:** 2026-09-20
**Author:** Supervisor (develcakes v2)
**Status:** Draft — for implementation
**Implements:** docs/specs/SPEC-MEMORY-WIDGET-RATCHET.md (v1 master, kept);
docs/post-mortems/2026-09-17-MEMRATCHET-POST-MORTEM.md (closure + baseline data)
**Depends on:** none (parallel with SPEC-04)
**Target branch:** main

> Architecture compliance: "every append-driven surface is bounded-memory"
> (architecture.md §Modules/Feed). v1 post-mortem measured OVER-budget windows (0.82 and
> 1.32 MB/min vs 0.5 budget) — this spec closes the gap the post-mortem deferred.

---

## 1. Overview

**Problem.** The v1 post-mortem closed P11 with 0.82 MB/min clean-window slope — over
the 0.5 MB/min budget — because the slope gate was deferred (adjudication: "slope never
waits on the count instrument"). The remaining ratchet: append-driven GTK surfaces
retain widgets/rows for every card ever added; long feed-heavy sessions grow monotonically.

**Solution.** Implement the generalized invariant: **bounded retention windows on every
append-driven surface**, with the Project Feed as the first surface converted:
- Feed view keeps a live window of **~300 cards** (configurable via settings);
- Cards beyond the window are **archived to the existing disk store**
  (utils/feed_store.py persists all cards already) and represented by a single
  "…N earlier" summary row at the top;
- Windowed teardown: widgets beyond the window are removed (unrefed) when new cards
  append past the cap.

**Scope**

| In | Out |
|---|---|
| ui/views/feed_tab.py live-window enforcement | SQLite feed rearchitecture |
| utils/feed_store.py retention config | Transcript store (SPEC-08) |
| Measurement harness rows | Activity drawer (P11 phases already bounded it) |
| Tests (window, archive accounting) | |

## 2. Changes by File

### ui/views/feed_tab.py — live window

Discovery: feed_tab.py (40,789 bytes) hosts the scrollable card list; FeedHandler
(ui/handlers/feed_handler.py, 134 KB) calls feed-store append + view add. The view's
card container is a `Gtk.Box` (cards appended in order; verified pattern from
`_add_card` usage in feed tests).

Add to the view class:

```python
_LIVE_WINDOW_DEFAULT = 300

@property
def live_window(self) -> int:
    # config: <config_dir>/feed-prefs.json already exists (verified in
    # .crabcakes listing) — read "live_window" if present
    ...

def _enforce_live_window(self) -> None:
    """Tear down widgets beyond the retention window (P11 invariant)."""
    overflow = self._card_widgets_count - self.live_window
    if overflow <= 0:
        return
    for _ in range(min(overflow, 64)):  # chunked teardown, no long UI stall
        oldest = self._cards_box.get_first_child()
        if oldest is None:
            break
        self._cards_box.remove(oldest)
        self._archived_count += 1
        self._card_widgets_count -= 1
    self._update_earlier_summary_row()
```

(Exact member names to be finalized against the file during implementation; the builder
must read `ui/views/feed_tab.py` and adapt names — this spec fixes the **behavior**: cap
300 default, chunked removal, summary row, archived count tracked. All cards remain in
`feed_store` disk persistence — verified: `feed.json` + `feed-updates.jsonl` are the
stores; the view is a projection.)

### utils/feed_store.py — retention config

Add `get_live_window()` / `set_live_window(n)` reading/writing the existing
feed-prefs.json (verified present in `.crabcakes/`). Clamp 50–5000. No store-format
change: disk keeps everything; the window is view-level.

### Measurement rows (post-mortem §13)

Add `tests/test_feed_retention.py`:
- synthetic append of 2,000 cards through the feed handler into the view (with GTK
  main-loop stubs per existing test patterns in tests/test_feed_*.py);
- assert live widget count ≤ window + summary row;
- assert disk store holds all 2,000 (no data loss — `load_all_cards` count).

**Files NOT changed:**
- `ui/handlers/feed_handler.py` — append path untouched; view enforces the window
- `utils/feed_store.py` serialization — format unchanged
- Activity drawer — already bounded by MEMRATCHET phases 1–11

## 3. Data Flow

Card appended → FeedHandler.add_card → feed_store persist (disk, complete) → view
append → `_enforce_live_window()` → widgets beyond cap torn down chunk-wise → summary
row shows archived count. Reload: view hydrates only the tail (last `live_window`
cards) + summary row from disk store.

## 4. File Change Summary

| File | Change | ~Lines | Risk |
|---|---|---|---|
| ui/views/feed_tab.py | window enforcement + summary row + tail hydration | +90 | med |
| utils/feed_store.py | window config accessors | +25 | low |
| tests/test_feed_retention.py | new | ~140 | — |

## 5. Implementation Order

1. Config accessors + tests.
2. View window enforcement + summary row + tail hydration + tests.
3. 2,000-card synthetic run: widget count flat after cap; disk count intact.
4. Full suite + ruff + pyright.

## 6. Acceptance Criteria

- [ ] Live widget count stays ≤ window (+1 summary row) under unbounded appends
- [ ] Disk store retains every card; reload hydrates tail + "…N earlier" row
- [ ] `live_window` configurable (feed-prefs.json), clamped 50–5000, default 300
- [ ] Teardown chunked (≤64 per pass) — no UI stall measurable in test harness
- [ ] Full pytest green, ruff clean, pyright clean

## 7. Edge Cases

| Case | Behavior |
|---|---|
| Filter/search active while overflow tears down | Enforcement skips when a filter is active (defer to next plain append) — document in code |
| User scrolls to summary row and clicks "load earlier" | Out of scope here (post-MVP enhancement); summary row is display-only |
| Window shrunk at runtime (config edit) | Next append pass enforces new cap; no eager teardown burst |
| Cards with live timers/animn (activity pulse) | Window teardown must cancel pending GLib sources on removed cards — follow MEMRATCHET phase patterns (source_remove guards, e.g. activity_handler.py:869 pattern) |

## 8. ARCHITECTURE.md Updates

§Modules/Feed — note implemented invariant + config key.
