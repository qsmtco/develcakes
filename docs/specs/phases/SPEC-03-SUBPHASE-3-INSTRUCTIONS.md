# SPEC-03 Sub-Phase 3 Instructions — 2,000-Card Retention Harness (the measurement)

**Spec:** docs/specs/SPEC-03-MEMORY-RATCHET.md §2 "Measurement rows" + §6 acceptance
**Prereqs:** SP1 (34d6260a) + SP2 (077bdd64) committed.
**Scope: exactly 2 files** — `tests/test_feed_retention.py` (the harness) and, only if
a probe below demands it, `ui/handlers/feed_handler.py` (a seam refactor). Nothing else.

## The env constraint (read first)

The spec's "through the real FeedHandler.add_card" path calls `build_feed_card`
(feed_handler.py:26 import) — real GTK, segfaults headless in this env (clean-HEAD-
proven, banked). The sanctioned seam: **patch `ui.handlers.feed_handler.build_feed_card`
to return `_StubWidget()`** — everything else in add_card runs for real: feed_store
persist (disk), seq assignment, `_card_widgets` bookkeeping, the eviction pass,
backlog, Load-More. This is the same philosophy as SP2's `_LiteFeedTab` (headless by
construction) but ONE level deeper because the harness must exercise the real add path.

## Probe FIRST (before writing the harness — report the result)

With build_feed_card patched and a `_LiteFeedTab` wired via `set_feed_tab`, run 100
synthetic cards through `add_card(card, persist=True)` on a tmp project. Report:
(a) 100 rows on disk (`load_all_cards` count — check feed_store for the actual API
name; it may be `load_feed` — use whatever exists), (b) widget-map length, (c) any
exception from the persist path (journal/compaction thresholds at 100 cards?). If
anything OTHER than build_feed_card breaks headless (e.g. update_card_badge import
side-effects), stop and report — that's the "seam refactor" case for feed_handler.py.

## The harness (new class TestTwoThousandCardRetention)

Seeds: tmp project dir, `set_live_window(proj, 300)` (explicit — pins the window the
acceptance criteria reference), handler wired as in SP2 tests. Append 2,000 cards via
the real `add_card` loop (batch calls sanctioned: `add_cards_batch` where the
bookkeeping is equivalent — if you use it, say why it's equivalent: same persist +
eviction funnel). Assert:

1. **Widget bound:** `len(handler._card_widgets) <= 300 + 1` (cap + Load-More row
   allowance; document the exact invariant you assert).
2. **Disk complete:** disk store holds all 2,000 (no loss — the projection promise).
3. **Accounting closes:** live widgets + backlog ≈ 2,000 (state the exact identity:
   `len(_card_widgets) + len(_backlog) + explicit-removals == 2000` — count removals
   via the stub tab's `removed` list; removed cards MUST be in backlog per
   `_evict_surplus_card_widgets`'s push-back).
4. **No widget leak:** python object count via a light `gc`-based widget-typename
   census before/after (assert stub-widget instance count bounded, not growing with
   all 2,000) — OR the simpler proxy: `_card_widgets` length already asserted; pick
   one and document.
5. **Speed guard:** full harness wall-time < 30s (regression tripwire for accidental
   O(n²) in the append/evict path).

Plus 3 targeted tests (small, fast):
6. `test_window_edge_exact_300` — 300 cards → 0 evicted; 301st → exactly 1 evicted.
7. `test_disk_survives_reload` — after the 2,000-run, new handler instance hydrates
   from disk (use the loader path that doesn't need GTK; if hydration itself needs
   build_feed_card, patch the same seam and document) → disk count still 2,000.
8. `test_compaction_interplay` — feed_store's compaction threshold
   (FEED_WINDOW_DEFAULT×1.25) vs 2,000 cards: does compact_feed prune disk below
   2,000? READ the compaction trigger; if it prunes to its own window, the harness
   must either use a tmp project sized so compaction doesn't fire, or assert the
   POST-COMPACTION disk count honestly (the disk window and the view window are
   different mechanisms — SPEC-03 §2 says "disk keeps everything" but compaction is
   feed_store's own older mechanism; document what actually happens, don't force it).

## Riders from SP2 audit (do these too)

9. R1-pin body fix: `test_configured_above_never_raises_cap` seed 150 widgets
   (not 120) so the eviction-body assert can fail under a raised-cap regression
   (re-run Debugger's `.debug/audit-scratch/sp2_r1pin_vacuity.py` logic mentally:
   post-fix, seed 150 + bugged cap 5000 → 150 remain → assert fails). Docstring
   updated to match.
10. Unescape `b"\\x00\\x01garbage not json"` → `b"\x00\x01garbage not json"` (:666).

## Verification (paste ALL, real runs)

```
.venv/bin/python -m pytest tests/test_feed_retention.py -q            # 38+ (35+3 main; riders amend existing)
.venv/bin/python -m pytest tests/test_feed_store.py -q                # 61
.venv/bin/python -m ruff check tests/test_feed_retention.py ui/handlers/feed_handler.py
.venv/bin/python -m ruff format --check tests/test_feed_retention.py
.venv/bin/pyright tests/test_feed_retention.py 2>&1 | tail -1
```

feed_handler.py ruff baseline 18 / pyright 0 — if your seam refactor touches it,
re-measure and hold. Test file: ruff-clean, format-clean, pyright errors ≤ the
pytest-import artifact only.

## COMPLETENESS
- [ ] Probe result reported BEFORE harness build
- [ ] Harness asserts 1–5 (+ tests 6–8, riders 9–10)
- [ ] Compaction interplay documented honestly (test 8)
- [ ] All 5 outputs pasted
- [ ] Deviations flagged
