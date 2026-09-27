# SPEC-07: R4 — Feedbar Removal

**Date:** 2026-09-20
**Author:** Supervisor (develcakes v2)
**Status:** IMPLEMENTED (2026-09-27; SP1 7103640f, SP2 cba48c85, SP3 aedff869)
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

> **§2 AMENDED 2026-09-25 (Supervisor, pre-flight verification against HEAD 3972ac9e).**
> The original sketch drifted from the code: (1) the handler calls FIVE feedbar methods,
> not three — `set_progress_pulse` and `pulse_progress` were missed; (2) `set_status_text`
> receives Pango markup and the pill's CSS class cannot be derived from text — the primary
> method carries the state; (3) the pill is a plain-text Gtk.Label with NO progress/visibility
> element, so fraction/hidden/pulse become documented no-ops. Method NAME `set_status_text`
> is preserved so existing `call_count` test assertions survive. Line numbers below are
> identifier-anchored; verified sites: window.py:36 (import), :256 (construct), :260 (arg),
> :773 (append).

### ui/handlers/activity_handler.py

Ctor becomes `__init__(self, status_target, main_content, GLib_module=None)`.
`status_target` duck-type (all five methods the handler calls today):

```python
set_status_text(text: str, state: str | None = None)  # primary: text→pill label, state→CSS class
set_progress_fraction(f: float)                       # no-op on the pill (no progress element)
set_progress_hidden(hidden: bool)                     # no-op
set_progress_pulse(enable: bool)                      # no-op
pulse_progress()                                      # no-op
```

All `_update_feedbar()` call sites renamed `_update_status()` (mechanical);
`_update_status()`/`_streaming_label()` build PLAIN text (Pango markup dropped —
target is a plain-text label). State-machine logic (states, timers, budgets,
signature skip-gating) is untouched.

### ui/views/chat_surface.py (from SPEC-06)

1. `set_activity_status(text: str, state: str | None = None)` on BOTH `ChatSurface` and
   `TextViewFallback`: text always applied; state given → CSS class swap via a
   6→class map (idle→idle, sending→thinking, reasoning→thinking, streaming→streaming,
   tool_use→tool, done→done, error→error); `pill-streaming`/`pill-done` CSS rules added.
   Existing `set_activity_pill` (4-state) unchanged.
2. `ActivityPillAdapter(resolver)`: resolver is a callable returning the ACTIVE surface
   (or None). Implements the 5-method duck-type above; caches last (state, text) and
   re-applies on surface identity change (tab switch catch-up); None surface → no-op.

### ui/handlers/chat_render_handler.py

Add read-only `surface_for_key(session_key)` → `self._surfaces.get(session_key)` or None.
(NOT `_surface_for` — that mounts/creates on miss; render path is per-250ms-tick and
must be side-effect-free.)

### ui/window.py

Remove the `FeedBar` import (:36), `self._response_status = FeedBar()` (:256), the
`feedbar=` ctor arg (:260), and `right_box.append(self._response_status)` (:773).
ActivityHandler constructed with `ActivityPillAdapter` whose resolver is
`lambda: self._chat_render_handler.surface_for_key(self._main_content.get_current_session_key())`.

### OUT OF SCOPE (naming collision guard)

`main_content.set_feed_bar_text`, `window._on_feed_bar_update`, and
`tests/test_*_settings_bar.py` are the PROJECT SETTINGS BAR — a different widget that
keeps its names. The R4 grep-zero gate applies to `FeedBar|feedbar|_response_status`
in activity-status contexts only; settings-bar sites are explicitly exempt.

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

**Sub-phased 2026-09-25 per the standing PM sizing directive (SPEC-06 SP5 pattern).**

- **SP1** — Adapter + surface seam: `set_activity_status` on both surface classes +
  CSS + `ActivityPillAdapter` in chat_surface.py; `surface_for_key` in
  chat_render_handler.py; new tests (red-first). No handler/test repoint yet.
- **SP2** — Handler repoint: ctor kwarg `feedbar=` → `status_target=`; `_update_feedbar`
  → `_update_status`; markup → plain text; the four test files repointed
  (`feedbar=` → `status_target=`, `_feedbar` → `_status_target`, FeedBar widget tests
  move to adapter tests). window.py NOT touched yet — window still constructs with
  the old kwarg? **No: window.py repoints in SP2 too** (suite green in one commit;
  feedbar.py still present but unreferenced by the activity path).
- **SP3** — Deletion: feedbar.py deleted; window.py unwired (import/construct/arg/append);
  grep gates zero; test_uirsp3 Phase-4 widget tests retired-with-disposition (subject
  died); ARCHITECTURE.md §Modules note.
- **SP4** — Close-out: full suite + ruff + pyright, post-mortem (mandatory 11-section),
  commit, push.

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
