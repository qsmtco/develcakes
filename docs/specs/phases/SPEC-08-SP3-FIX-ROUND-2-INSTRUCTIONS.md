# SPEC-08 SP3 FIX ROUND 2 — re-audit BUG #1–#4 + flake headroom (Debugger, 2026-09-27)

**Builder rule:** `prompts/steelFramedCodeWriter.md` — invoke it, apply every rule.
**My note:** BUG#1 is my fix-brief's gap — I specified the earned-rename for the
append branch only; the skip branch kept the COUNT predicate. Same class, sibling
path. This round closes coverage on BOTH branches.

## BUG #1 (HIGH) — skip branch renames on COUNT; coverage is the predicate

`rows_before >= len(messages)` is totality, not coverage. Phantom high-seq rows
(count matches, index missing) and multi-epoch `load_all` inflation (epoch-0 has 5,
bump_epoch, epoch-1 has 2, file=4 → count 7 ≥ 4, current epoch holds 2/4) both
rename-and-destroy.

**Fix — store-side coverage helper + both branches use it:**

Add to `utils/transcript_store.py`:

```python
def covers(self, session_key: str, upto: int) -> bool:
    """True iff the CURRENT epoch holds a row at every seq in [0..upto].

    Coverage, not count (SP3 audit BUG#1): cardinality matches while indexes
    are missing (phantom high-seq rows) and load_all() spans all epochs
    (prior-epoch rows inflate the count). One SQL COUNT against the current
    epoch with seq <= upto == upto + 1 — exact, no gaps possible (UNIQUE
    makes seqs distinct within the epoch, so count==upto+1 ⟺ full coverage).
    """
```

(one `SELECT COUNT(*) FROM turns WHERE session_key=? AND epoch={_CUR_EPOCH}
AND seq <= ?` — compare to `upto + 1`; `upto < 0` → True vacuously.)

Migration:
- **skip branch:** replace `rows_before >= len(messages)` with
  `store.covers(sk, len(messages) - 1)`.
- **append branch:** replace `rows_after = len(store.load_all(sk)); rows_after <
  len(messages)` with the same predicate: `if not store.covers(sk,
  len(messages) - 1):` → error+keep+retry (the existing BUG#1 path).

**Tests** (extend the wm-ahead test class):
1. `test_skip_branch_count_collision_keeps_file` — the auditor's exact probe:
   3 rows (0,1,2) + `append_turn(seq=9)` phantom → wm=9, load_all count=4,
   file=4 → NOT renamed, error recorded, store unchanged.
2. `test_epoch_inflation_does_not_cover` — epoch-0 5 rows, `bump_epoch()`,
   epoch-1 2 rows, file=4 → NOT renamed (current epoch covers 0..1 only).
3. Keep the existing wm-ahead append-branch test green (it must now pass through
   `covers`).

## BUG #2 (MEDIUM) — init-order race on the drop-guard

`_start_store_migration()` runs at :559; `self._running` first assigned at :572.

**Fix:** in `AgentRuntime.__init__`, BEFORE the `_start_store_migration()` call:
`self._running = False` and `self._stopped = False` (dedicated flag). `stop()`
sets `self._stopped = True` FIRST. The completion guard reads `self._stopped`
only (not `_running`) — construction-time completion dispatches normally,
pre-start completion dispatches (not-yet-stopped is not stopped), post-stop
drops. Keep the log line's wording accurate to the three states.

**Test** `test_sweep_completing_during_init_dispatches`: monkeypatch
`_migrate_store_async` to invoke `on_complete` synchronously → construct
AgentRuntime → no AttributeError, idle_add dispatched (RecordingGLib ≥ 1),
`_running` is False but `_stopped` False → not dropped.

## BUG #3 (issue) — banner gate omits errors

`run_store_migration_once` gate: `if (stats["migrated"] > 0 or stats["aborted"]
or stats["errors"]) and on_complete:` — an all-corrupt sweep (migrated=0,
aborted=False, errors=3) fires the card ("3 sessions failed, will retry next
launch" is the SP4 banner text; the callback contract carries errors now).
**Test** `test_all_corrupt_fires_banner`: 3 corrupt files → on_complete fired
exactly once, errors populated, migrated==0.

## BUG #4 (suggestion) — comment precision

Fix the "one COUNT read feeds three decisions" comment to describe reality:
skip branch reads coverage via `covers`; append branch reads `rows_before`
(watermark/coverage precheck) + post-append `covers`. One-line reword + the
build-report claim correction rides your report.

## Flake headroom (registered item — cheap close now)

The auditor proved load-only starvation at exactly 5.006s under host IO
contention, and `busy_timeout=15000` → 6/6 clean. Fold it in: bump
`busy_timeout=5000` → `15000` in `TranscriptStore.__init__` (update the SP1 pin
`test_busy_timeout_set` to 15000 with a comment citing the load-probe evidence).
Worst-case stall only matters under pathological contention; real writes are ms.

## Verification (paste full output — pyright MANDATORY, xvfb full suite)

```
env -u DISPLAY .venv/bin/python -m pytest tests/test_migration.py tests/test_agent_persistence.py tests/test_transcript_store.py -v
.venv/bin/python -m pyright agent/persistence.py agent/runtime.py utils/transcript_store.py tests/test_migration.py
python -m ruff check agent/persistence.py agent/runtime.py utils/transcript_store.py tests/test_migration.py
xvfb-run -a .venv/bin/python -m pytest tests/ -q --ignore=tests/test_enforcement.py --ignore=tests/test_mcp_config.py -p no:cacheprovider 2>&1 | tail -3
```

## COMPLETENESS (mandatory)

- [ ] BUG#1: `covers()` helper + BOTH branches on the coverage predicate
- [ ] BUG#1 tests: count-collision (phantom seq) + epoch-inflation — both keep the file
- [ ] BUG#2: flag init before migration + `_stopped` guard + during-init dispatch test
- [ ] BUG#3: errors in the banner gate + all-corrupt test
- [ ] BUG#4: comment reword
- [ ] busy_timeout 15000 + pin update
- [ ] Full battery green incl. pyright + full suite
- Related issues found, not fixed: <list or none>

Deviations carry a one-sentence rationale each.
