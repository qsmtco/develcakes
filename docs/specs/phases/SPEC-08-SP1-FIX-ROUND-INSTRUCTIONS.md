# SPEC-08 SP1 FIX ROUND — audit BUG #1/#2/#3/#4 (Debugger, 2026-09-27)

**Builder rule:** `prompts/steelFramedCodeWriter.md` — invoke it, apply every rule.
**Context:** SP1 core functionally correct in-process, but the audit's multi-instance
probe dropped 500–999 turns per trial and the commit-failure probe flushed a "failed"
turn. Supervisor ruling on BUG #1: **BEGIN IMMEDIATE (b) + pinned contract (a)** —
the module owns the discipline; busy_timeout then serializes the second writer.

## BUG #1 (HIGH) + BUG #4 (rollback) — one restructure

Make transactions explicit and immediate. Pattern (all four write methods:
`append_turn`, `delete_session`, `bump_epoch`; plus `close`'s commit):

1. In `__init__`: `sqlite3.connect(..., isolation_level=None)` — autocommit mode;
   ALL transaction control becomes explicit. (With the default isolation_level,
   implicit DEFERRED transactions begin at first DML — that's the race: two
   connections can both SELECT-MAX before either holds the write lock.)
2. Every write method body becomes:

```python
with self._lock:
    self._ensure_open()
    try:
        self._conn.execute("BEGIN IMMEDIATE")   # acquire write lock UP FRONT
        ... existing SELECT/INSERT/UPSERT logic unchanged ...
        self._conn.execute("COMMIT")
        return seq  # (or deleted count / new epoch)
    except Exception:
        self._conn.rollback()                    # BUG #4: never leave a dangling tx
        raise
```

   `BEGIN IMMEDIATE` + `busy_timeout=5000` = the second INSTANCE waits for the
   first's COMMIT instead of racing the SELECT and dying on UNIQUE.
3. Read methods (`tail`, `load_all`, `session_watermark`): no BEGIN needed (single
   statements are atomic; autocommit reads are fine).
4. `close()`: keep idempotent; its commit can stay bare (nothing pending in
   autocommit mode; harmless).
5. `rollback()` itself may raise if no tx is active — guard: `try: rollback()
   except sqlite3.OperationalError: pass` inside the except branch (belt-and-braces;
   BEGIN IMMEDIATE failing to acquire leaves no tx).
6. Module docstring: scope the claim — "single-writer discipline lives HERE:
   within one process via the instance lock; ACROSS processes/instances via
   BEGIN IMMEDIATE + busy_timeout (the second writer waits, never races)."

## BUG #1 tests (the pinned contract)

- `test_two_instances_serialize_zero_lost` — TWO `TranscriptStore` objects on the
  SAME tmp DB file, one session, 2 threads × 300 appends each (thread↔instance
  1:1, `threading.Barrier` start). Assert: zero exceptions, 600/600 rows via a
  FRESH third connection, seqs contiguous 0..599 (set-equality), watermark 599.
- `test_two_instances_interleave_distinct_sessions` — same setup, distinct
  sessions: 300+300, each session contiguous.

## BUG #2 test (use-after-close teeth)

- `test_use_after_close_raises` — after `close()`, ALL 8 public methods
  (append_turn, tail, load_all, delete_session, session_watermark, bump_epoch,
  close-exempt — close must NOT raise — so: the 6 that must raise + close no-op)
  → `pytest.raises(RuntimeError)` for the 6; document close's idempotence in the
  same test.

## BUG #3 test (tail cross-epoch teeth)

- Extend `test_bump_epoch_starts_new_seq`: after the bump + one new-epoch append,
  assert `tail(sk, 10)` returns ONLY the new-epoch turn (1 row, epoch 1), while
  `load_all` still returns all 3.

## BUG #4 test (commit-failure injection)

- `test_failed_commit_leaves_no_trace` — monkeypatch the connection's COMMIT to
  raise `sqlite3.OperationalError("injected")` ONCE (a wrapper with a counter);
  call `append_turn` → propagates; then from a FRESH connection on the same file,
  the turn is ABSENT; then restore and append again → succeeds, seq allocated
  correctly (no double-write, no flush of the failed row).

## Mutation proof (mandatory, paste each)

Re-run the audit's surviving mutations against the NEW suite — each must now die:
- M13 (`_ensure_open` → `return`) — killed by BUG #2 test.
- M4 (drop `AND epoch = {_CUR_EPOCH}` from tail) — killed by BUG #3 teeth.
- M-deferred (your BEGIN IMMEDIATE → removed/deferred begin) — killed by the
  two-instance test (run it 3×; report trial variance honestly — the deferred
  mutation's failure count varied 1–2 errors in the audit's repro; if a single
  run of 300+300 can EVER pass deferred, raise the count until it can't, and say so).
- M-norollback (your `except: rollback` removed) — killed by the commit-injection
  test (the failed row must not appear later).

## Verification (paste full output)

```
env -u DISPLAY .venv/bin/python -m pytest tests/test_transcript_store.py -v
python -m ruff check utils/transcript_store.py tests/test_transcript_store.py
env -u DISPLAY .venv/bin/python -m pytest tests/test_agent_persistence.py -q
```

(The suite grows 13 → 17: +2 two-instance, +1 use-after-close, +1 commit-injection,
with the BUG #3 teeth folded into the existing epoch test.)

## COMPLETENESS (mandatory)

- [ ] BUG #1/#4 restructure: isolation_level=None + BEGIN IMMEDIATE + rollback-on-fail (4 write paths)
- [ ] BUG #1 docstring: cross-instance claim scoped + mechanism named
- [ ] test_two_instances_serialize_zero_lost + distinct-sessions variant
- [ ] BUG #2: test_use_after_close_raises (6 raise + close no-op)
- [ ] BUG #3: tail cross-epoch teeth folded into epoch test
- [ ] BUG #4: commit-injection test (absent-from-fresh-connection + next-append-ok)
- [ ] Mutation proof: M13 / M4 / M-deferred / M-norollback each killed (pasted)
- [ ] Full battery green; ruff clean; persistence untouched
- Related issues found, not fixed: <list or none>

Deviations carry a one-sentence rationale each.
