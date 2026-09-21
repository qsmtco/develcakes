# UIRESP2 Phase 1 — Background + Coalesced Feed Writes

**Work unit:** SPEC-UI-RESPONSIVENESS-2-PHASES-1-3, Phase 1 only (§2.1)
**Spec:** `docs/specs/SPEC-UI-RESPONSIVENESS-2-PHASES-1-3.md` — READ IT IN FULL FIRST. §2.1 is your contract; §10 has build-time notes.
**Baseline:** 21fb08c (spec commit). Baseline full-suite failure set: 40 failures at 352caf7 — your work must not add to or change that set.
**Discipline:** `prompts/steelFramedCodeWriter.md` — invoke it. Red-first: every new test demonstrated failing BEFORE the fix lands.

## Scope — 3 files

1. `ui/handlers/feed_handler.py`
2. `ui/window.py`
3. `tests/test_feed_handler.py` (new test class only — do not modify existing tests)

## CRITICAL: interim shape (Phase 3 adds more later)

The spec's §2.1.2 snippet shows the FINAL three-phase drain (with the compact branch). **In Phase 1 you implement it WITHOUT the compactions machinery**: no `_persist_compactions` state, no `_enqueue_compaction`, no compact branch in the drain, no `_surface_prune_card`. Your drain has TWO phases: (1) deferred in-place, (2) queue. The `_persist_loop` drained-check drops the compactions term. Everything else is verbatim from the spec.

## Edits

**E1 — imports (`ui/handlers/feed_handler.py`):** add `import os` to the module imports (the spec verified it's missing — audit r5 #1).

**E2 — `__init__` state (after `self._lock` at :82):** verbatim from spec §2.1.1, minus `self._persist_compactions`.

**E3 — new methods (after `update_card`, before `_update_card_visual`):** `_ensure_persist_writer`, `_enqueue_card_update`, `_persist_loop`, `_drain_persist_queue`, `shutdown_persist_writer` — verbatim from spec §2.1.2 with the Phase-1 interim shape:
- `_persist_loop`: `drained = (not self._persist_queue and not self._persist_deferred)`
- `_drain_persist_queue`: two phases only (deferred in-place, then queue). Keep the `import os`-using size-scaled join in `shutdown_persist_writer` and the three-way exit log.

**E4 — Edit A (`update_card` :963):** replace the sync persist block (:1015-1021, the `from utils.feed_store import update_feed_card` local import + call) with the enqueue block from spec §2.1.2 Edit A. Widget rebuild below it unchanged.

**E5 — Edit B (accept/reject :1431, :1492):** replace the two `feed_store.update_feed_card(project_path, card_id, {"accepted": True/False})` calls with `self._enqueue_card_update(...)` same args.

**E6 — Edit C (`ui/window.py` :585-592):** append `self._feed_handler.shutdown_persist_writer(),` as the final tuple element of the project-close lambda chain.

**E7 — Edit D (`ui/window.py`):** in `MainWindow.__init__` near the `"realize"` connect (:66): `self.connect("close-request", self._on_close_request)` + the `_on_close_request` handler verbatim from spec §2.1.2 Edit D.

## Tests (RED FIRST — tests/test_feed_handler.py, new class `TestBackgroundPersistWriter`)

Follow spec §5 Phase 1 red-test list + invariants 1-8 (adjust for interim shape — no compaction tests yet):
1. immediacy: `update_card` wall time <50 ms with a large in-memory fixture (structural assertion: no disk I/O — patch `feed_store.update_feed_card` with a sleeper and assert it's NOT called on the main-thread path before writer drain)
2. coalescing: N enqueues for one card → 1 writer call; final state = last update
3. poison no-strand: one failing entry + one good entry → good persists in the same pass
4. retry cap: 3 failures → ERROR drop (assert via caplog)
5. fresh budget: enqueue pops deferred entry for the key — a queue entry's first failure is tries=1 (fresh budget, no comparison)
6. deferred in-place: tries preserved across passes (not reset)
7. bounded shutdown: stop → ≤1 drain pass → exit; drop-during-stop logged; three-way exit log
8. close-then-reopen: stopped writer restarts on next enqueue; new generation resets tries
9. tri-state None: `update_feed_card` returning None → INFO log + drop, never deferred (mock returning None)
10. malformed entry: empty project/card in queue → WARNING drop

Use a recording GLib fake for writer-thread tests where needed; the existing sync `MockGLib` stays untouched. Writer behavior tests can call `_enqueue_card_update` + `_drain_persist_queue` directly (deterministic) or run the real thread with joins (bounded by `shutdown_persist_writer`).

## Verification (paste full output)

```bash
PYTHONDONTWRITEBYTECODE=1 xvfb-run -a python3 -m pytest tests/test_feed_handler.py -q
PYTHONDONTWRITEBYTECODE=1 xvfb-run -a python3 -m pytest tests/test_feed_store.py tests/test_low12_13_feed.py -q
PYTHONDONTWRITEBYTECODE=1 xvfb-run -a python3 -m pytest tests/test_window.py -q 2>/dev/null || echo "test_window.py does not exist — skip"
```

Plus: `grep -n "from utils.feed_store import update_feed_card" ui/handlers/feed_handler.py` → 0 matches; `grep -c "feed_store.update_feed_card(" ui/handlers/feed_handler.py` → 0 matches (all persist calls now go through the queue). `grep -n "import os" ui/handlers/feed_handler.py` → 1 match. pyflakes on both changed files: 0 undefined-name.

## Report

COMPLETENESS checklist per edit (E1-E7) + each red test with its pre-fix failure output, files-changed with line numbers, full pytest outputs, grep proofs. Flag related issues — do not fix silently. Note any spec drift (anchor off >10 lines).

STOP after Phase 1 — Debugger audits before Phase 2.
