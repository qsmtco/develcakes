# UIRESP2 Phase 2 — O(1) Update Journal + Compaction + Bounded Lock

**Work unit:** SPEC-UI-RESPONSIVENESS-2-PHASES-1-3, Phase 2 only (§2.2)
**Spec:** `docs/specs/SPEC-UI-RESPONSIVENESS-2-PHASES-1-3.md` — READ IT IN FULL. §2.2 is your contract; §10 build-time notes.
**Baseline:** a3c95c2 (Phase 1 landed). Phase-1 known interim race closes HERE: the journal path makes updates existence-independent (D4), and the tri-state converts "card gone" from retryable False to non-retrying None.
**Discipline:** `prompts/steelFramedCodeWriter.md`. Red-first. Interim shape: compaction `window` wiring arrives in Phase 3 — call sites pass `window=None` for now where the spec shows `FEED_WINDOW_DEFAULT` (the constant itself is defined in Phase 3; define it NOW in Phase 2 so Phase 3 needs no signature churn — spec §2.3.1).

## Scope — 2 files

1. `utils/feed_store.py` (the bulk)
2. `tests/test_feed_store.py` (new tests + 1 rewrite; existing tests otherwise untouched)

`ui/handlers/feed_handler.py` is OUT OF SCOPE this phase (the writer calls `feed_store.update_feed_card` with the same signature; the tri-state return is additive).

## CRITICAL: signature change

`update_feed_card(project_path, card_id, updates) -> bool | None` (was `-> bool`). True = journaled or written; False = write failure (append AND legacy failed — lock timeout/OSError); None = legacy path ran, card not found. The Phase-1 writer already handles the tri-state (`ok is False` → RuntimeError → defer; `ok is None` → INFO drop).

## Edits (spec §2.2 is the authority — verbatim where shown)

**E1 — constants + rate-limit state (§2.2.1):** `JOURNAL_FILENAME`, `JOURNAL_COMPACT_THRESHOLD = 500`, `_LOCK_TIMEOUT_SEC = 2.0`, `_COMPACT_MIN_INTERVAL = 60.0`, `_compact_last`/`_compact_rl_lock`; `import threading` added to feed_store. Also define `FEED_WINDOW_DEFAULT = 2000` now (Phase-3 constant, defined early per the note above).

**E2 — bounded lock (§2.2.2):** `_acquire_lock(path, timeout=_LOCK_TIMEOUT_SEC) -> tuple | None` — deadline loop, `os.close(fd)` + WARNING on timeout. DELETE the unbounded blocking fallback at :112.

**E3 — caller lock discipline (§2.2.3):** every mutation holds the flock; `None` handling per spec (load: size-scaled timeout then lock-free WARNING; writes: skip + WARNING); `save_feed` GAINS the lock; `_release_lock` guarded by `if fd is not None`.

**E4 — journal primitives (§2.2.4):** `_UPDATABLE_FIELDS = frozenset({"accepted","reviewed","metadata","body"})` (D3 — body now persists); `_journal_path`, `append_card_update` (flock-held; plain json.dumps NO default=; TypeError/OSError → WARNING + False; `"a"` mode; prepend `\n` if non-empty file lacks trailing newline; gitignore on first journal creation; non-dict metadata skip + WARNING), `_replay_journal`, `_journal_line_count`, `_parse_cards`, `_apply_overlay` (shared helpers — per-key metadata merge; non-dict metadata overlay values skipped + WARNING).

**E5 — `load_feed` (§2.2.5):** snapshot + journal replay under ONE lock hold; size-scaled timeout `min(60.0, 10.0 + size/1_000_000)` (getsize guarded); overlay applied via `_apply_overlay`.

**E6 — journal-first `update_feed_card` (§2.2.6):** tri-state; append-ok ⇒ True regardless of compaction outcome (compact wrapped in try/except WARNING); False only when append AND legacy fail.

**E7 — `_update_feed_card_legacy`:** current body verbatim + tri-state returns (True found+written / False write-failure incl. lock-timeout skip / None not-found) + allowed set `_UPDATABLE_FIELDS`.

**E8 — `compact_feed` (§2.2.7):** flock-held fold (inline `_parse_cards` — never calls load_feed; no lock re-entry); `_apply_overlay`; window param honored but Phase-2 call sites pass `window=None`; compact JSON write; journal truncate; rate-limit timestamp record on success (`with _compact_rl_lock: _compact_last[project_path] = time.monotonic()`).

**E9 — `append_feed_card` trigger (§2.2.8):** releases its lock, then `_maybe_compact(project_path)` — rate-limited (60 s per project, timestamp BEFORE size check); NO flock re-entrancy (test this).

**E10 — `_atomic_write_json` compact param (§2.2.9):** `compact: bool = False`; no indent, NO default= when compact. Feed snapshot callers pass compact=True; prefs unchanged.

## Tests (RED FIRST — tests/test_feed_store.py)

New tests per spec §5 Phase 2 + invariants 1-10 (window tests are Phase 3 — use `window=None` here):
1. O(1) journal append: 9,500-card fixture within 3× empty-feed time (structural: no feed.json read — patch or spy)
2. journal round-trip: append_card_update → load_feed → card updated (accepted/reviewed/metadata/body — body is the D3 regression test)
3. replay idempotent: replay twice → identical
4. torn tail: truncate the final line → prior records applied + exactly one WARNING
5. compaction folds + truncates under one lock: journal ≥ threshold → compact → snapshot contains updates, journal empty
6. lock timeout: externally held flock → `_acquire_lock` returns None within bound (never unbounded)
7. load under held lock: size-scaled timeout honored (small file → ~10 s budget: assert via monkeypatched time or stub)
8. serialization failure: non-JSON-native value (e.g. an object) in updates → WARNING + False → legacy fallback runs (card persists via to_dict) — never corrupt, never crash
9. non-dict metadata: at append_card_update → skip + WARNING; at _apply_overlay → skip + WARNING
10. True-despite-compact-failure: append ok, compact_feed raises → update_feed_card still True
11. tri-state None: legacy path, card not found → None (rewrite `test_update_nonexistent_returns_false` per D4 — also assert journal-path nonexistent returns True)
12. save_feed gains lock: no behavior change single-threaded; nested-acquire guard holds
13. no nested flock acquire anywhere in feed_store (instrument `_acquire_lock` with a counter/thread-local reentry flag)
14. rate limit: two `_maybe_compact` triggers within 60 s → one compact
15. `make_feed(n)` fixture builder (shared, Phase 3 reuses)

Also verify the 13 existing tests pass unchanged (save the D4 rewrite) — `test_low12_13_feed.py` must stay green (permissions/gitignore, no indent-format assertions).

## Verification (paste full output)

```bash
PYTHONDONTWRITEBYTECODE=1 xvfb-run -a python3 -m pytest tests/test_feed_store.py tests/test_low12_13_feed.py tests/test_feed_handler.py -q
```
Expect: all green (Phase-1's 161 + your new store tests; the interim-race ERROR logs from Phase 1's writer should DISAPPEAR once the journal path is live — verify and report).
```bash
grep -n "fcntl.flock(fd, fcntl.LOCK_EX)$" utils/feed_store.py   # → 0 (unbounded fallback gone)
PYTHONPATH=/tmp/pf-venv2 python3 -m pyflakes utils/feed_store.py | grep -c "undefined name"  # → 0
python3 -c "from utils import feed_store; print(feed_store.FEED_WINDOW_DEFAULT)"  # → 2000
```

## Report

COMPLETENESS per edit E1-E10 + each red test with pre-fix failure, full pytest output, grep proofs, and explicitly: **evidence the Phase-1 interim race is closed** (a test: enqueue update for a card whose append hasn't landed → journal returns True → update survives to load_feed). Spec drift flags. STOP after Phase 2.
