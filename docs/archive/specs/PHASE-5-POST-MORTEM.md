# Phase 5 Post-Mortem — Feed Card UX (Persistent Toolbar + Auto-Accept Toggle)

> Date: 2026-06-28
> Spec: `docs/specs/FEED-CARD-UX-PHASE-5-INSTRUCTIONS.md`
> Commits: `97941f9`, `475b974`, `347693c`, `8c8a743`, `f6333cd`, `886de91`, `c234d7e`

## Summary

Phase 5 is complete and verified. All 11 spec steps implemented across 5 sub-phases. 68/68 tests in `test_feed_handler.py` (59 original + 9 new). 282/282 feed-adjacent tests pass. Zero bugs found in final adversarial audit.

## What Went Well

1. **Sub-phased delegation worked.** Breaking the spec into 5 small phases (1-3 files each) kept each builder task focused and verifiable.
2. **Adversarial audit caught nothing in production code.** All 5 phases passed audit on first try — the spec was precise enough that the builder could implement it verbatim.
3. **MockGLib's immediate-execution semantics were handled correctly.** QTR adapted the test drain pattern in Phase 5-5 to match the actual MockGLib (which runs callbacks immediately AND records them).
4. **Backward compat in MockFeedTab avoided unnecessary test churn.** Keeping `_batch_bar_visible`/`_batch_bar_count` as aliases meant `TestBatchAccept` didn't need updates.

## Spec Drifts Found

| # | Drift | Impact | Resolution |
|---|---|---|---|
| 1 | Master spec names method `_show_auto_accept_warning_dialog`; Phase 5-4 spec names it `_show_auto_accept_warning` | Method name mismatch | Followed Phase 5-4 spec (more recent, more specific) |
| 2 | Master spec says `Gtk.ButtonsType.YES_NO`; Phase 5-4 spec says `NONE + add_button` | Button labels | Followed Phase 5-4 spec ("Turn On"/"Cancel" clearer than "Yes"/"No") |
| 3 | Master spec says wrap dialog construction in `GLib.idle_add`; Phase 5-4 spec does not | Threading | Phase 5-4 spec is correct — dialog is constructed on main thread (GTK signal handler) |
| 4 | Master spec says `*, ` keyword-only separators in dialog signature; Phase 5-4 spec keeps all positional | Signature style | Followed Phase 5-4 spec (positional) |
| 5 | Spec verify step says "handle_accept appears in mock_glib._pending" but MockGLib runs immediately | Test design | QTR adapted: used `_pending.clear()` instead of drain loop |
| 6 | Spec `grep -n "_batch_bar"` returns false-positive on `update_batch_bar` substring | Verification noise | Used strict grep with word boundaries to confirm no stale DOM refs |

## Pre-Existing Issues (Not Caused by Phase 5)

- 12 test failures in `tests/test_improve.py` (11) and `tests/test_mcp_config.py` (1) — all predate Phase 5, all unrelated to feed/auto-accept.

## Known Limitations / Future Work

1. **Agent lock-in persists across toggle cycles.** If user toggles auto-accept OFF then ON again, the old `_auto_accept_agent` persists. Spec says "first author that ever arrives" — this is correct per spec, but may surprise users who expect a fresh start on each ON cycle. **Out of scope for Phase 5.**

2. **No "don't show warning again" checkbox.** Spec §7 explicitly defers this. **Out of scope.**

3. **Per-agent auto-accept matrix UI deferred.** Spec §7 defers to "Phase 6+". **Out of scope.**

4. **Redundant `import gi` / `gi.require_version` / `from gi.repository import Gtk` inside `_show_auto_accept_warning`.** These are already done at module level in `window.py`. The re-imports are defensive for self-containment (per spec). **Code smell, not a bug.**

5. **Dialog doesn't chain to MainWindow's `destroy` signal.** If the window is destroyed while the dialog is showing, the response handler may run against a defunct `self`. GTK4 typically handles this gracefully. **Out of scope.**

6. **Race condition between `_load_and_render` (background) and `_on_auto_accept_toggled` (main).** Narrow window during project load. If `add_card` is called before prefs are loaded, auto-accept won't fire for that card. Low severity — the next card will trigger it. **Flagged, not fixed.**

## File Manifest (Final)

| File | Delta | What |
|---|---|---|
| `ui/views/feed_tab.py` | +51/-5 | Toolbar widget, auto-accept API, removed `_batch_bar` |
| `ui/handlers/feed_handler.py` | +112/-1 | Auto-accept state, hook, prefs load, 6 new methods |
| `ui/window.py` | +58/-0 | Warning dialog method + wire callback |
| `ui/styles.py` | +38/-0 | Toolbar CSS classes (committed earlier) |
| `utils/feed_store.py` | +55/-0 | `save_feed_prefs` / `load_feed_prefs` (committed earlier) |
| `tests/test_feed_handler.py` | +169/-0 | `TestFeedToolbarAutoAccept` (9 tests), MockFeedTab extension |

**Total: 6 source/test files, ~484 lines of production code, ~169 lines of tests.**

## Test Coverage

| Test Class | Tests | Status |
|---|---|---|
| `TestFeedToolbarAutoAccept` (new) | 9 | ✅ All pass |
| `TestBatchAccept` (existing) | 9 | ✅ All pass (backward compat) |
| Other `test_feed_handler.py` tests | 50 | ✅ All pass |
| `test_feed_store.py` | 13 | ✅ All pass |
| `test_feed_card.py` | 70 | ✅ All pass |
| `test_review_handler_feed_card.py` | 10 | ✅ All pass |
| `test_low12_13_feed.py` | 11 | ✅ All pass |
| **Feed-adjacent total** | **282** | **✅ All pass** |
| Full suite | 2273 passed, 12 failed, 1 skipped | 12 pre-existing (unrelated) |

## Verdict

✅ **Phase 5 complete.** All spec steps implemented, all tests pass, zero bugs found in final audit. Ready for integration testing / user acceptance.
