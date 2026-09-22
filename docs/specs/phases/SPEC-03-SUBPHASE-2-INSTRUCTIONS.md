# SPEC-03 Sub-Phase 2 Instructions — Wire the Window: config → handler eviction cap

**Spec:** docs/specs/SPEC-03-MEMORY-RATCHET.md §2/§6/§7 · **Parent:** SPEC-03-SUBPHASES.md
**Prereq:** SP1 committed (34d6260a) — accessors live in feed_store.
**Scope: exactly 2 files** — `ui/handlers/feed_handler.py`, `tests/test_feed_retention.py`.
Nothing else. No view (feed_tab) changes.

## Adjudicated design rulings (binding — read twice)

**R1 — Default 300 NEVER raises live memory.** The binding constraint is the post-mortem
budget (0.5 MB/min; measured 0.82). Handler's cap today: `MAX_LIVE_CARD_WIDGETS = 120`
(feed_handler.py:38). Wiring rule:
```
effective_cap = min(MAX_LIVE_CARD_WIDGETS, get_live_window(project_path))
```
The view cap can only go BELOW 120 via config, never above. `LIVE_WINDOW_DEFAULT=300`
is the *retention-card* default (SP3's harness reads it), not a widget-cap raise. Rationale
comment in code: post-mortem slope 0.82 over 0.5 budget — the default must not add widgets.

**R2 — KEEP_NEWEST_CARDS stays a constant.** No config coupling. It is an internal
eviction-safety floor, not a user surface. (Sub-phase plan's "floor ratio" idea is
RESCINDED — simpler and preserves MEMRATCHET round-3..5 behavior exactly.)

**R3 — Read at eviction-call time.** Mirror the "MEMRATCHET §2.1 read at call time"
comment at :36-38: `_evict_surplus_card_widgets` resolves the cap per pass from
`feed_store.get_live_window(self._project_paths[active])` — never caches in __init__.
Handle missing project path → fall back to MAX_LIVE_CARD_WIDGETS (current behavior).

**R4 — Main-thread stall budget (Debugger advisory).** get_live_window does file I/O +
no lock (read-only) → fast (µs-ms). BUT the eviction pass must not acquire the prefs
flock. Confirm: get_live_window takes NO lock (read path only). If you find it needs
one, stop and report — don't silently lock the main thread.

**R5 — Failure isolation.** Any exception from get_live_window inside the eviction pass
→ log + use MAX_LIVE_CARD_WIDGETS (never break the append/evict path; config is
non-critical). The read is tolerant already (SP1), but belt-and-braces per SPEC-02
lessons: card of failure paths.

## Task

In `_evict_surplus_card_widgets` (feed_handler.py:~1944): replace the
`len(self._card_widgets) <= MAX_LIVE_CARD_WIDGETS` check (and the `target`
computation ~:1961) with the R1 formula under R3/R4/R5. Keep everything else
(victim selection, F10 viewport guard, scroll compensation, backlog, Load-More
rebuild) untouched. One helper, e.g. `_effective_live_window(self) -> int`, is
sanctioned if it keeps the eviction pass readable; docstring cites R1 rationale.

## Tests (add to tests/test_feed_retention.py — new class TestEvictionWindowWiring)

Read tests/test_feed_handler.py:45-180 mock patterns first (MockGLib, widget doubles,
eviction surface). Keep tests headless. Minimum 8:
1. `test_default_effective_cap_is_120` — no prefs file → cap 120 (R1: min(120,300)).
2. `test_configured_below_reduces_cap` — set_live_window(80) → cap 80; eviction fires
   at >80 widgets.
3. `test_configured_above_never_raises_cap` — set_live_window(9000→clamps 5000) → cap
   stays 120. THE R1 pin.
4. `test_missing_project_falls_back` — handler without project paths → cap 120.
5. `test_get_live_window_exception_falls_back` — patch get_live_window to raise →
   cap 120, eviction proceeds, no raise (R5).
6. `test_cap_read_per_pass_not_cached` — set 80, run pass, set 120, run pass → second
   pass uses 120 (proves R3 call-time read).
7. `test_eviction_still_respects_keep_newest` — cap 60, KEEP_NEWEST_CARDS=40 → victims
   never touch newest 40 (R2).
8. `test_prefs_file_corrupt_falls_back` — corrupt prefs → cap 120 (R3 tolerance).

**Rider (banked suggestion):** add `@pytest.mark.skipif(os.geteuid() == 0, ...)` to
SP1's `test_readonly_lock_file_returns_false_no_raise` while you're in the file.

## Verification (paste ALL, real runs)

```
.venv/bin/python -m pytest tests/test_feed_retention.py -q          # 35+ expected (27+8)
.venv/bin/python -m pytest tests/test_feed_handler.py -q            # eviction regression
.venv/bin/python -m pytest tests/test_feed_store.py -q
.venv/bin/python -m ruff check ui/handlers/feed_handler.py tests/test_feed_retention.py
.venv/bin/python -m ruff format --check tests/test_feed_retention.py
.venv/bin/pyright ui/handlers/feed_handler.py 2>&1 | tail -1
```

Measure + report feed_handler.py ruff/pyright baselines BEFORE your change.
NOTE: tests/test_feed_handler.py may hit the banked gi/cairo segfault when combined
with test_feed_card.py — run files SEPARATELY always. Env: .venv/bin/python.

## COMPLETENESS
- [ ] Baselines measured + reported (ruff/pyright on feed_handler.py)
- [ ] R1–R5 implemented (each cited in a code comment)
- [ ] 8 wiring tests + root-skipif rider
- [ ] All 6 outputs pasted
- [ ] Deviations flagged
