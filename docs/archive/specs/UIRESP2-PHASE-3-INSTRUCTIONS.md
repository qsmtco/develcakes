# UIRESP2 Phase 3 — Sliding-Window Pruning + Compaction Triggers + Prune Surfacing

**Work unit:** SPEC-UI-RESPONSIVENESS-2-PHASES-1-3, Phase 3 only (§2.3)
**Spec:** `docs/specs/SPEC-UI-RESPONSIVENESS-2-PHASES-1-3.md` — READ IT IN FULL. §2.3 is your contract; §10 build-time notes; the audit log at the top records every accepted deviation.
**Baseline:** c623196 (Phase 2 landed). `FEED_WINDOW_DEFAULT = 2000` already defined; `compact_feed` already accepts `window` (currently ignored with a debug log — you implement the pruning).
**Discipline:** `prompts/steelFramedCodeWriter.md`. Red-first. COMPLETENESS checklist mandatory.

## Scope — 4 files

1. `utils/feed_store.py` — retention rules inside `compact_feed`; trigger wire-ups
2. `ui/handlers/feed_handler.py` — `_persist_compactions` state + compact branch in the drain + `_enqueue_compaction` + `_surface_prune_card` + `add_card(persist=)` + load-time trigger
3. `tests/test_feed_store.py`, `tests/test_feed_handler.py` — new tests
4. `docs/ARCHITECTURE.md` — §8 grep-targeted update (final phase of the loop)

## Edits

**E1 — retention rules in `compact_feed` (spec §2.3.2):** when `window is not None`: pin rules (`accepted is not None`; `metadata.get("needs_review")` or `metadata.get("needs_approval")`; `card_type == "git_commit"`); keep `[-window:]` newest; additionally pin matching cards outside the slice; prune the rest; return pruned count; WARNING log with pinned count. The existing `window=None` path (Phase-2 behavior: no pruning, return 0) must remain intact for the no-window calls.

**E2 — `update_feed_card` threshold trigger:** `compact_feed(project_path, window=None)` → `window=FEED_WINDOW_DEFAULT` (the Phase-2 comment at that site says Phase 3 supplies this).

**E3 — `_maybe_compact`:** already calls with `window=FEED_WINDOW_DEFAULT`? Verify; if it passes None, wire it to the window.

**E4 — handler state (feed_handler `__init__`, after the Phase-1 block):** add `self._persist_compactions: list[tuple[str, int]] = []` (spec §2.1.1).

**E5 — `_enqueue_compaction` (spec §2.1.2, external-replace semantics):** dedupe by REPLACE (list comprehension filtering same path + append `(path, 0)`), under `_persist_queue_lock`; `_ensure_persist_writer()` + `wakeup.set()`.

**E6 — compact branch in `_drain_persist_queue`:** add the leading compactions phase per spec §2.1.2's final form: snapshot+clear under lock at pass start; iterate; `compact_feed(task_path, window=FEED_WINDOW_DEFAULT)`; `if pruned: self._surface_prune_card(path, pruned, window)`; failure handling: stop → drop ERROR; `tries+1 >= 3` → drop ERROR; else only-if-absent re-enqueue `(path, tries+1)`. Phase order: compactions FIRST, then deferred, then queue (matches spec).

**E7 — `_surface_prune_card(project_path, pruned, window)` (spec §2.3.5 verbatim):** writer-thread method; `FeedCardData` system card; `_ui` closure via `GLib.idle_add` with active-project guard; `add_card(card, persist=False)`; `copy.deepcopy(card)` on the main thread; tiny persist thread → `append_feed_card(project_path, snapshot)`. `import copy` added to module imports.

**E8 — `add_card(persist: bool = True)` keyword:** signature + persist condition `if project_path and not self._loading and persist:`. All existing callers positional on card_data — no breakage.

**E9 — load-time trigger (spec §2.3.4):** in `_load_and_render` after `cards = feed_store.load_feed(project_path)`: `if len(cards) > FEED_WINDOW_DEFAULT * 1.25:` log + `self._enqueue_compaction(project_path)`.

**E10 — ARCHITECTURE.md (spec §8):** `grep -n "feed.json\|feed_store\|append_feed_card\|update_feed_card" docs/ARCHITECTURE.md` — update every feed-persistence section: background writer + journal + window model, the uniform flock rule, pin rules, compaction triggers + rate limit, file inventory (`feed-updates.jsonl`), the documented rate-limit module-state deviation in feed_store's docstring contract note.

## Tests (RED FIRST)

**tests/test_feed_store.py** (new class `TestWindowPruning` + additions):
1. pin rules: pending/needs_review/needs_approval/accepted/git_commit survive pruning beyond the window
2. `seq_num` monotonic across compaction (make_feed fixture; after prune, new cards continue numbering — handler rebuild is feed_handler's; here assert the snapshot's max seq survives)
3. window=None unchanged: compact with no window prunes nothing (regression guard for the Phase-2 path)
4. pruned count returned + WARNING logged with pinned count
5. all-pinned edge: prune count 0, everything kept

**tests/test_feed_handler.py** (new class `TestWindowCompaction`):
6. load-time trigger: `len(cards) > 2500` → `_enqueue_compaction` called (spy/stub) — and NOT called at/below threshold
7. sentinel isolation: enqueue compaction + update together; assert `update_feed_card` never receives the compaction's project as card_id (spy)
8. compact branch in drain: failing compact re-enqueues only-if-absent; external enqueue replaces with fresh tries=0; retry cap 3 → ERROR drop
9. prune card surfaced: recording GLib fake — `add_card` NOT called before `fire()`, called after; AST structural test: every `add_card` call inside `_surface_prune_card` lies within a nested function def (the `_ui` must be a `def`, not lambda)
10. prune card persisted despite `_loading=True`; suppressed when project closed before the idle callback fires
11. `add_card(persist=False)` skips the persist thread (structural: no `append_feed_card` call)
12. shutdown drained-check now includes `_persist_compactions` (spec §2.1.2 final form)

## Verification (paste full output)

```bash
PYTHONDONTWRITEBYTECODE=1 xvfb-run -a python3 -m pytest tests/test_feed_store.py tests/test_low12_13_feed.py tests/test_feed_handler.py -q
PYTHONPATH=/tmp/pf-venv2 python3 -m pyflakes utils/feed_store.py ui/handlers/feed_handler.py | grep -c "undefined name"   # → 0
grep -n "window=None" utils/feed_store.py   # threshold site must now pass FEED_WINDOW_DEFAULT
grep -c "add_card" ui/handlers/feed_handler.py  # sanity
```
Plus the §8 ARCHITECTURE.md grep targets you actually updated (paste the grep + the sections touched).

## Report

COMPLETENESS per edit E1-E10 + red evidence per test + full outputs + the ARCHITECTURE.md diff summary. Spec drift flags. STOP after Phase 3 — final audit + post-mortem follow.
