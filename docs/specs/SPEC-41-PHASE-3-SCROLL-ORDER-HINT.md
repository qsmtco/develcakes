# SPEC-41 Phase 3 (WU 10.3) — Scroll reset on project switch, Load More order, read hint

**Parent spec:** `docs/specs/SPEC-41-FEED-POST-AUDIT-FIXES.md` — read §DISCOVERY
and §2 "Phase 3" first. This file is the complete brief.

**Why (three small defects, all user-visible now that the document is the
only feed):**
- (a) `FeedSurface.set_cards` (:118) resets the document maps but NOT
  `_doc_near_bottom` / `_doc_y`. Project B inherits project A's scroll
  offset AND A's follow-off state — so B opens at A's pixel offset and B's
  new cards land below the fold permanently. The reader looks "stuck".
- (b) `_load_more` (ui/feed/load.py:275) iterates `reversed(page)` and calls
  `prepend_html_card` → `FeedSurface.upsert(prepend=True)` →
  `_order.insert(0, cid)`. Each insert lands at the FRONT, so the page
  renders NEWEST-first while `render_feed_document` documents "fragments are
  oldest first" and `set_cards` documents "cards is oldest-first". The seq
  badges make the inversion visible (Debugger probe: `15, 14, 13 …`).
- (c) `agent/tools.py` (:515) — the byte-window continuation hint computes
  `line + content.count("\n")`. When the window ends MID-LINE the partial
  line's newline is not counted, so the hint points at a line the model
  already has (probe: `line=1 limit=12` → hint says `line=2`, the next
  unread line is 3). The same hint's `offset=` alternative is correct.

## Deliverables

1. `ui/views/feed_surface.py` `set_cards` (:118) — reset the scroll state
   with the document: `self._doc_near_bottom = True` and `self._doc_y = None`.
   A new project starts at the top and follows.
2. `ui/feed/load.py` `_load_more` HTML branch (:275) — make the page land
   oldest-first. With `prepend` inserting at index 0, the CALL order must be
   newest → oldest (so the oldest insert ends up at index 0). `page` is
   newest-first, so iterate `page` directly instead of `reversed(page)`.
   Verify against `render_feed_document`'s oldest-first contract and
   `set_cards`'s docstring — do not guess.
3. `agent/tools.py` (:515) — correct the continuation arithmetic: when the
   body does NOT end with a newline the last line is incomplete → next
   unread line is `line + newlines + 1`; when it DOES end with a newline →
   `line + newlines`. Keep the `offset=` alternative unchanged.

## Tests
- `tests/test_feed_surface_scroll.py` (extend): `set_cards` resets
  `_doc_near_bottom` to True and `_doc_y` to None, even when the previous
  state was "scrolled up".
- Load More ordering: a test that drives `_load_more` with a known page and
  asserts the document order is oldest-first (seq numbers ascending) —
  mirror the existing retention harness's document double
  (`tests/test_feed_retention.py` `_LiteHtmlSurface`/`_LiteHtmlFeedTab`).
- `tests/test_tools.py` (extend): the continuation hint after a mid-line
  window points at the next UNREAD line; after a newline-terminated window
  it points at the next line.

## Read budget (hard limit)
- `ui/views/feed_surface.py` windows: :110-140 (set_cards), :130-160 (upsert)
- `ui/feed/load.py` windows: :250-300 (_load_more)
- `agent/tools.py` windows: :495-525 (the hint)
- ONE existing test file per area (grep for the fixture, read one each)

## Gate (mandatory protocol)
- Targeted: `xvfb-run -a .venv/bin/pytest tests/test_feed_surface_scroll.py tests/test_feed_retention.py tests/test_tools.py -q` — paste the tail.
- Full suite BACKGROUNDED (`nohup xvfb-run -a .venv/bin/pytest -q > /tmp/full41c.log 2>&1 &`), report `gate running pid N`, STOP. Supervisor polls. NEVER poll in-turn.
- Baseline: 47F / 5311P / 5S + earlier phases' tests + yours.
- Lint: no new findings vs HEAD.

## Report
Per-file summary, targeted output, gate pid, tool-call count. Then STOP —
the Debugger re-audit of all three phases follows.
