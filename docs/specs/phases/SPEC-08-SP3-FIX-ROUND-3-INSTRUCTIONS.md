# SPEC-08 SP3 FIX ROUND 3 — covers() lower bound + start() latch (micro-round)

**Builder rule:** `prompts/steelFramedCodeWriter.md` — invoke it, apply every rule.
**Supervisor note:** the missing `seq >= 0` floor and the false docstring biconditional
came from MY round-2 brief — implemented as specified. This round corrects the spec
of the helper, not the builder's work.

## BUG #1 (HIGH, blocking) — `covers()` has no lower bound

`COUNT(*) WHERE seq <= upto` counts negative seqs; `{0,1,-1}` with `upto=2` →
count 3 → "covered" while index 2 is absent → skip branch renames → data loss.

**Fix (store, utils/transcript_store.py:436):**
1. Query gains `AND seq >= 0`: `SELECT COUNT(*) FROM turns WHERE session_key = ?
AND epoch = {_CUR_EPOCH} AND seq >= 0 AND seq <= ?` (bind order unchanged — sk, sk, upto).
2. Docstring: correct the claim — "count == upto+1 with seqs constrained to
[0..upto] and UNIQUE distinctness ⟺ exact coverage of every index in [0..upto]."
3. Schema belt-and-braces: add `CHECK(seq >= 0)` to the turns CREATE TABLE.
   NOTE in a comment: CREATE TABLE IF NOT EXISTS does not ALTER existing DBs —
   the query floor is the real fix; the CHECK guards fresh databases only.

**Tests (store + migration):**
- `test_covers_negative_seq_row_is_not_coverage` — raw-INSERT a seq=-1 row
  (the SP1 corrupt-seeding pattern) alongside {0,1}: `covers(sk, 2)` is False;
  `covers(sk, 1)` stays True (the real rows still cover 0..1).
- Migration-level: extend the wm-ahead/collision test class with
  `test_negative_seq_keeps_file` — seed {0,1,-1}, file=3 → NOT renamed, error
  recorded, store unchanged, idempotent second run.

## BUG #3 (issue) — `start()` doesn't clear `_stopped`

One line in `start()`: `self._stopped = False` (with a comment: restart reopens
the dispatch window; stop() re-latches). **Test** `test_restart_clears_stopped`:
start → stop → start → invoke the completion guard (RecordingGLib or inline
receiver) → dispatches (not dropped), `_stopped` is False.

## BUG #2 (issue, register-not-code) — trust boundary documented

The skip branch's coverage predicate is INDEX-coverage only; content equality is
the wrapper guard's job at save time (dual anchor) and stable-ids post-MVP. Add a
3-line comment at the skip branch stating exactly that boundary (the auditor's
"document" option). No code.

## BUG #4 (suggestion, cheap) — during-init idle_add pin

Add one test (or extend the existing during-init test) constructing the runtime
WITH `GLib=` injected so `_dispatch` takes the real idle_add path during
`__init__` → assert the recording GLib saw ≥ 1 idle_add (pins the wired path the
current deviation-skipped test doesn't).

## Verification (paste full output — pyright MANDATORY, xvfb full suite)

```
env -u DISPLAY .venv/bin/python -m pytest tests/test_migration.py tests/test_agent_persistence.py tests/test_transcript_store.py -v
.venv/bin/python -m pyright agent/persistence.py agent/runtime.py utils/transcript_store.py tests/test_migration.py
python -m ruff check agent/persistence.py agent/runtime.py utils/transcript_store.py tests/test_migration.py tests/test_transcript_store.py
xvfb-run -a .venv/bin/python -m pytest tests/ -q --ignore=tests/test_enforcement.py --ignore=tests/test_mcp_config.py -p no:cacheprovider 2>&1 | tail -3
```

## COMPLETENESS (mandatory)

- [ ] BUG#1: `seq >= 0` floor + corrected docstring + CHECK (fresh-DB note) + 2 tests
- [ ] BUG#3: `start()` clears `_stopped` + restart test
- [ ] BUG#2: trust-boundary comment (no code)
- [ ] BUG#4: during-init idle_add pin (GLib= injected)
- [ ] Full battery green incl. pyright + full suite
- Related issues found, not fixed: <list or none>
