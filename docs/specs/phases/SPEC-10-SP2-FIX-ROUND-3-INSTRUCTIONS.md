# SPEC-10 SP2 Fix Round 3 — lock ALL root git critical sections + stale-card emit out of lock

**Supervisor:** Supervisor; **Builder:** Coder; **Auditor:** Debugger
**Scope:** `ui/handlers/review_handler.py` + `tests/test_review_queues.py`.
Binding ruling: **D3 REV 4a** in `docs/specs/phases/SPEC-10-PREFLIGHT-DECISIONS.md`.

---

## BUG #1 (HIGH) — accept_changes / start_review / reject_changes bypass the project lock

Debugger quantified (`scratch/dbg_sp2fix2_acceptchanges_race2.py` + 2 more):
accept_all ∥ accept_changes → PARTIAL 12/20, stranded 12/20, index.lock ×17;
per-agent ∥ accept_changes → 9/20; double accept_changes → index.lock ×13;
accept_all ∥ start_review → 3/20. Root: the REV 4 ruling says ALL public
entry points; the round-2 instruction scoped the edit to
`_accept_agent_queue_sync` only (my instruction gap — the ruling text was
correct).

### Fix (D3 REV 4a enumeration)

Take `self._project_lock_for(project_name)` around the root git critical
section of:

1. **`accept_changes`** — the whole `_do()` body from `stage_all` through
   the commit + state update. The `_drain_pm_queue` calls inside it are
   `_queue_lock`-only — safe under the project lock (nesting order:
   project_lock → queue_lock, the ONLY allowed order; deadlock-scan
   verified by the auditor).
2. **`start_review`** — stage + commit (+ the enqueue after; the enqueue is
   `_queue_lock`-only — safe).
3. **`reject_changes`** — the `checkout_paths` call section.

Rules:
- NO new lock acquisition inside these sections beyond `_queue_lock`
  (verify with a scan of every call between acquire and release).
- GLib idle callbacks scheduled inside the lock still fire later on the
  main thread — fine (they never take the project lock).
- The lock must NOT be held across the confirmation dialog or any GLib
  main-loop blocking (there is none today in these paths — keep it that
  way).

### BUG #2 (LOW) — stale-drop card emits inside the project lock

The stale-worktree drop card (`review_handler.py:830–836` area) emits while
holding `_project_accept_lock`. Latent re-entrancy (non-reentrant Lock +
future same-thread re-entry). Fix: accumulate stale outcomes during the
loop, emit their cards AFTER the `with` block — same pattern as the summary
card (which correctly emits outside at ~890).

### BUG #3 (LOW) — lock-dict leak: DO NOT FIX (ruling)

No eviction on project close. Evicting a lock an in-flight accept holds →
close→reopen→setdefault creates a second live lock for one project → the
stampede returns. Accepted cost: ~40 bytes per distinct project name
(single-user desktop). Ruling recorded in D3 REV 4a. **Add one comment line
on `_project_accept_locks` stating the no-eviction ruling so no future
reader "fixes" the leak.**

### Tests (RED-first)

1. `test_accept_all_vs_accept_changes_no_race` — real repo, one agent root
   entry (dirty tree → real commit) ∥ `accept_changes` (own dirty file);
   assert max-concurrent stage_all == 1 (thread-recording pass-through),
   no PARTIAL, no index.lock errors, both resolve.
2. `test_double_accept_changes_no_race` — two concurrent accept_changes on
   one project: max stage_all == 1, no index.lock error in any emitted
   text/card (RED today: ×13/20).
3. `test_start_review_vs_accept_all_no_race` — start_review ∥ accept_all:
   max stage_all == 1, both complete (checkpoint sha set + queue drained).
4. `test_no_card_emitted_inside_project_lock` — instrument
   `_emit_feed_card` with a lock-held probe (e.g. a wrapper that records
   whether the project lock is `locked()` at call time on this handler
   instance): assert every emit happens unlocked. (BUG#2 RED: the stale
   card currently fires held=True.)

RED proofs: shapes 1–3 are the auditor's reproduced rates (paste yours);
shape 4 RED = the current stale-card emit (held=True observed by the
auditor's instrumentation).

## Do NOT change

- `_accept_agent_queue_sync`'s existing lock usage (whole-loop hold) —
  unchanged.
- `_drain_pm_queue` internals; `_queue_lock` discipline.
- BUG C (shared-root `-A` cross-attribution) — registered, out of scope.
- The exact-string REV 3 match.

## Battery (paste all)

- `.venv/bin/python -m pytest tests/test_review_queues.py tests/test_review_handler_feed_card.py tests/test_review_state.py tests/test_review_log.py tests/test_stop_all.py -q` (xvfb-run)
- ruff multiset + pyright 0 on both files
- RED proofs for the 4 tests
- Stress rates post-fix: run your equivalents of the auditor's
  accept_changes_race2 + gap_quant shapes ≥10 iterations each; expect
  0 PARTIAL / 0 stranded / 0 index.lock.

## COMPLETENESS (mandatory)

- [ ] BUG#1: lock on accept_changes + start_review + reject_changes — diff
      hunks + RED (3 race tests)
- [ ] BUG#2: stale-card emit moved out of lock — diff hunk + RED (held-probe)
- [ ] BUG#3: no-eviction ruling comment — diff hunk
- [ ] Battery + ruff/pyright + stress rates
- [ ] Related issues found, NOT fixed (flagged)

Invoke `prompts/steelFramedCodeWriter.md`. Please write and report when done.
