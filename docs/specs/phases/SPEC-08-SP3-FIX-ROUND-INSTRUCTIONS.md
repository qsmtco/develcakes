# SPEC-08 SP3 FIX ROUND — audit BUG #1–#6 (Debugger, 2026-09-27)

**Builder rule:** `prompts/steelFramedCodeWriter.md` — invoke it, apply every rule.
**Standing rulings unchanged** (store=append-only ledger; diverged=JSON-only; load-gap
+ banner + on_progress wiring = SP4). This round: BUG#1–#5 fully; BUG#6 guard-only.

## BUG #1 (HIGH) — ignored append_delta return → rename on a no-op write (DATA LOSS)

The wm-ahead state (corrupt wm, future load-sync regression, manual edit) makes
`append_delta` start past every row → returns -1 having written NOTHING → the file
renames anyway → the only copy of the missing turns is gone, reported as success.

**Fix — rename is EARNED by verified progress:**

In `migrate_conversations_to_store`, per session, after the append branch:

```python
rows_after = len(store.load_all(sk))
if rows_after < len(messages):
    # append_delta was a no-op (watermark ahead of rows — the declared
    # defense state). NOT migrated: no rename, collect into errors.
    result["errors"].append((sk, f"watermark ahead of rows: wm={store.session_watermark(sk)} rows={rows_after} file={len(messages)}"))
    continue
os.rename(...)  # only reached when the store provably holds >= file count
```

(The pre-append COUNT skip stays — this is the fall-through path's teeth.)

**Test** `test_watermark_ahead_session_not_renamed_no_loss`: seed the probe's exact
shape (3 rows + append_turn(seq=9, "phantom") → wm=9, file=10) → migrate → file
NOT renamed, store unchanged (4 rows), error recorded naming the session, migrated
count does NOT include it, second run behaves identically (idempotent refusal).

## BUG #2 (issue) — test global leak: `_store_singleton` not restored

`test_init_with_flag_runs_sweep_async` runs the REAL singleton path; finally restores
`_store_override` only. The singleton (pointing at a deleted tmp DB) leaks into
subsequent tests — the incident class, test-side.

**Fix:** capture BOTH globals before, restore BOTH in finally (mirror the
TestSingletonPath pattern). Add a post-test assertion inside the test itself:
`persistence._store_singleton is None` after the finally-block restore runs (i.e.,
assert at teardown via a fixture or trailing check in the test body).

## BUG #3 (issue) — corrupt DB aborts the whole sweep, latch eats the retry, no card

`_get_store()` at :532 is OUTSIDE the per-session try; corrupt DB → the function
raises → `_migrate_store_async` logs → latch already True → silent, zero sessions,
no banner signal. Spec §7 says: corrupt → fallback + card.

**Fix:** move the store acquisition INSIDE a try at the top of
`migrate_conversations_to_store`: on failure, return a stats dict with
`errors=[("<store>", repr(e))], migrated=0, turns=0, seconds=elapsed, aborted=True`
— a return, never an exception. `run_store_migration_once` passes it to
`on_complete` (the SP4 banner will render "migration failed — JSON untouched,
will retry next launch" from `aborted`). Add `"aborted": False` to the normal
return shape. **Latch semantics:** on `aborted=True`, UNSET the latch (return-
and-retry next launch is the spec's posture — a transient corrupt state shouldn't
permanently silence the feature).

**Test** `test_corrupt_db_aborts_cleanly_with_retry`: garbage db → migrate returns
aborted=True (no raise), errors[0][0] == "<store>", no files renamed, THEN fix the
db bytes (delete + recreate) → migrate again → succeeds.

## BUG #4 (issue) — `turns` metric lies on resumed sessions

`result["turns"] += len(messages)` counts file size, not rows written.

**Fix:** count actual progress: `rows_before` (the COUNT check you already do) →
`result["turns"] += rows_after_append - rows_before`. Use the same `rows_after`
load you add for BUG#1 — one extra `load_all` per session at most, or track via
the delta length actually appended. **Test:** extend the resume test to assert
`stats["turns"] == 20` (the resumed 20), not 40.

## BUG #5 (suggestion) — stale comment

runtime.py:109-110 "The app entry point sets it" → "Set by the OPERATOR before
launch (opt-in; the app does not set it — see main.py's pointer to the SP4 ruling)."

## BUG #6 (guard-only part) — dispatch after stop

In `_start_store_migration`'s on_complete closure (or `_dispatch` itself): guard on
`self._running` — if the runtime has stopped, log-and-drop instead of calling
GLib.idle_add into a torn-down loop. ONE guard; the on_progress consumption + card
wiring stays SP4 per the ruling.

**Test** `test_migration_callback_dropped_after_stop`: construct runtime, stop it,
invoke the captured on_complete path → no raise, no idle_add (patched GLib records
zero calls), a log line exists.

## Report accuracy (not a code bug)

Your COMPLETENESS said 16 tests; the file collects 14. Correct the record in your
report (which tests exist by name) — the audit caught the discrepancy.

## Verification (paste full output — pyright MANDATORY, xvfb for full suite)

```
env -u DISPLAY .venv/bin/python -m pytest tests/test_migration.py tests/test_agent_persistence.py tests/test_transcript_store.py -v
.venv/bin/python -m pyright agent/persistence.py agent/runtime.py tests/test_migration.py
python -m ruff check agent/persistence.py agent/runtime.py tests/test_migration.py
xvfb-run -a .venv/bin/python -m pytest tests/ -q --ignore=tests/test_enforcement.py --ignore=tests/test_mcp_config.py -p no:cacheprovider 2>&1 | tail -3
```

## COMPLETENESS (mandatory)

- [ ] BUG#1: progress-verified rename + wm-ahead test (not-renamed, no-loss, idempotent)
- [ ] BUG#2: both globals restored + teardown assertion
- [ ] BUG#3: store acquisition in-try, aborted stats shape, latch unset on abort, retry test
- [ ] BUG#4: turns = rows written (resume test asserts 20)
- [ ] BUG#5: comment corrected
- [ ] BUG#6 guard: _running drop-guard + after-stop test (progress/card = SP4)
- [ ] Test-count record corrected (14 + your new ones, named)
- [ ] Full battery green incl. pyright + full suite
- Related issues found, not fixed: <list or none>

Deviations carry a one-sentence rationale each.
