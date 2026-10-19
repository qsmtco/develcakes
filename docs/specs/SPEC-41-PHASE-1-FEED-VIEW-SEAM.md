# SPEC-41 Phase 1 (WU 10.1) — Feed view seam: fix the project-switch crash

**Parent spec:** `docs/specs/SPEC-41-FEED-POST-AUDIT-FIXES.md` — read §DISCOVERY,
§2 "Phase 1", §6 acceptance criteria before starting. This file is the
complete brief for this phase.

**Why this phase is blocking:** every project switch/close raises
`AttributeError` in `ui/handlers/feed_handler.py:762` → `ui/views/feed_tab.py`
`remove_card` (deleted in `da7913f6`, never re-routed). The raise aborts the
window's project-opened callback tuple (`ui/window.py:604-631`), so
CrabWatch, prompt handlers, the activity drawer and the feed bar never
update. The suite stayed green because the test MOCK still has
`remove_card` — a mock mirroring a world that no longer exists.

## Deliverables

1. `ui/views/feed_tab.py`:
   - Re-add `remove_card`, `append_card`, `prepend_card`, `replace_card`,
     `get_card_container` as compat no-ops (exact signatures + bodies in
     parent spec §2 Phase 1(a)). `remove_card` forwards to
     `remove_card_html(card_id)`; the other four return None / None.
   - `hide_card_buttons` (:868): no-op body — no `_cards_by_id` read.
   - `is_above_viewport` (:544): return False immediately — no
     `_card_container` read.
2. `ui/handlers/feed_handler.py`: re-route the two legacy call sites
   (:586 idle `_remove` closure, :762 `clear_project`) to the document API
   (`remove_card_html`) or to the correct HTML equivalent — read the
   enclosing function before choosing.
3. `tests/test_feed_switch_regression.py` (NEW): drive the REAL `FeedTab`
   (not a mock):
   - project switch / project close raise nothing;
   - `remove_card` forwards to `remove_card_html` (monkeypatch the surface
     and assert the call);
   - `hide_card_buttons` / `is_above_viewport` return without raising on a
     widget-less tab, and `is_above_viewport` is False;
   - grep-style assertion: no production reference to `_cards_by_id` /
     `_card_container` remains in `ui/views/feed_tab.py`.
   Build the tab by copying the pattern from ONE existing test that
   constructs a real `FeedTab` (grep `tests/` for `FeedTab(`, read one
   file only).

## Read budget (hard limit)
- `ui/views/feed_tab.py` windows: :1-40 (docstring + class), :250-275
  (document API), :540-575 (is_above_viewport), :860-900 (hide_card_buttons)
- `ui/handlers/feed_handler.py` windows: :575-600, :750-775, :950-985
- ONE existing test file that constructs a real FeedTab
- `tests/test_feed_handler.py` — grep for `MockFeedTab` to see the mock
  (do not copy it; that is the trap this phase exists to close)

## Gate (mandatory protocol)
- Targeted: `xvfb-run -a .venv/bin/pytest tests/test_feed_switch_regression.py tests/test_feed_handler.py tests/test_feed_retention.py -q` — paste the tail.
- Full suite: launch BACKGROUNDED
  (`nohup xvfb-run -a .venv/bin/pytest -q > /tmp/full41a.log 2>&1 &`),
  verify it started, report `gate running pid N`, then STOP. The Supervisor
  polls and gates. NEVER poll or sleep-tail inside the turn.
- Baseline: 47F / 5311P / 5S (63-baseline minus the purge-deleted tests).
  Your new test file is the only expected delta. Failure set must diff
  identical otherwise.
- Lint: `ruff check` on both production files — no NEW findings versus HEAD.

## Report
Per-file change summary, targeted output, gate pid, tool-call count. Then
STOP — Phases 2 and 3 are separate GOs.
