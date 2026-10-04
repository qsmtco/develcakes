# SPEC-10 SP2 Fix Round 2 — cross-invocation serialization + pm drain on ALL exit paths

**Supervisor:** Supervisor; **Builder:** Coder; **Auditor:** Debugger
**Scope:** `ui/handlers/review_handler.py` + `tests/test_review_queues.py`.
Binding rulings: **D3 REV 4** (per-project accept serialization) and
**D8 REV 3** (all session-resolving exits drain pm) in
`docs/specs/phases/SPEC-10-PREFLIGHT-DECISIONS.md`.

---

## Finding A (HIGH) — concurrent public accept calls stampede the repo

Debugger stress probes (`scratch/probe_sp2fix_stress.py`, 10 iters):
double `accept_all_queues` → partial cards 8/10, cross-attribution 10/10;
`accept_all` + per-agent → stranded 6/10. Root: each public call spawns its
own worker; no serialization of the git critical section across invocations.

### Fix (D3 REV 4)

In `__init__`:

```python
        # D3 REV 4: serializes the git critical section per project across
        # ALL public accept entry points. Two workers may snapshot-iterate
        # concurrently (the dict lock guards the queues), but two
        # stage/commit sections never overlap on one repo.
        self._project_accept_locks: dict[str, threading.Lock] = {}
```

In `_accept_agent_queue_sync`, wrap the whole per-entry loop body (the
stage/commit critical section — from the classification through the dequeue
of the last entry) with the project's lock:

```python
        with self._project_lock_for(project_name):
            ...existing for entry in snapshot: loop...
```

Helper (setdefault under a tiny guard or just `setdefault` — dict
`setdefault` is atomic in CPython; disclose your choice):

```python
    def _project_lock_for(self, project_name: str) -> threading.Lock:
        return self._project_accept_locks.setdefault(project_name, threading.Lock())
```

A second concurrent call blocks until the first finishes, then its snapshot
is (likely) empty → the friendly "No pending checkpoints" message. That is
the intended UX for a double-click.

**Scope note:** hold the lock for the WHOLE per-agent loop (not per-entry)
— per-entry release would let an interleaved accept_all's agent B slip
between A's entries; whole-loop keeps per-project strictly serial. The pm
drain (`_drain_pm_queue`) does NOT take this lock (it only holds
`_queue_lock`; no git ops).

### Tests (RED-first)

1. `test_double_accept_all_no_race` — two threads calling
   `accept_all_queues` concurrently on a real repo with 2 agents' root
   entries (dirty tree for one so a real commit happens). Assert: both
   queues drain; NO "PARTIAL" card in any emitted text/card; max concurrent
   `stage_all` == 1 (thread-recording pass-through, same technique as the
   existing serialization test).
2. `test_accept_all_plus_per_agent_no_race` — `accept_all_queues` +
   `accept_agent_queue(other_agent)` concurrently: same asserts.

RED proofs: with the current code these are the stress shapes —
partial cards and/or stranded entries appear (nonzero rate is enough to
prove RED; state the observed rate).

## Finding B (MEDIUM) — diff-read-error branch doesn't drain pm

### Fix (D8 REV 3)

`accept_changes`' diff-read-error `_reset_state` (~line 422–430): add
`self._drain_pm_queue(project_name)` alongside the existing reset (mirrors
the other two branches at 451/492).

### Test

3. `test_diff_read_error_drains_pm` — start_review (pm ≥ 1) → force the
   diff-read error (patch `gitpython.Repo` to raise inside the
   `repo.index.diff("HEAD")` try — or however the existing tests force it;
   disclose) → assert pm pending == 0 and the session reset. RED: current
   code leaves pm == 2 (Debugger's probe P3 shape).

## Do NOT change

- The exact-string REV 3 match, `_drain_pm_queue` internals, the
  single-worker accept_all loop (internally sequential — unchanged).
- BUG C (`-A` cross-attribution on shared root trees) — REGISTERED
  pre-existing, not this round (D2 superset property bounds it; exact root
  attribution is an evolution item).

## Battery (paste all)

- `.venv/bin/python -m pytest tests/test_review_queues.py tests/test_review_handler_feed_card.py tests/test_review_state.py tests/test_review_log.py tests/test_stop_all.py -q` (xvfb-run)
- ruff multiset (no new classes) + pyright 0 on both files
- RED proofs for the 3 tests
- Re-run Debugger's stress shapes from `scratch/probe_sp2fix_stress.py` if
  importable, or replicate: 10 iterations, report strand/partial rates
  (expect 0/10 post-fix)

## COMPLETENESS (mandatory)

- [ ] Finding A: per-project lock + helper — diff hunk + RED (2 stress tests)
- [ ] Finding B: error-path drain — diff hunk + RED
- [ ] Battery + ruff/pyright outputs
- [ ] Stress-rate evidence (0/10 post-fix)
- [ ] Related issues found, NOT fixed (flagged)

Invoke `prompts/steelFramedCodeWriter.md`. Please write and report when done.
