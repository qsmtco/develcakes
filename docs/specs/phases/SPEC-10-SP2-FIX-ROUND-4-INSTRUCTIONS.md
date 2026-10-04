# SPEC-10 SP2 Fix Round 4 — lock the two remaining checkout paths + daemon-bleed hardening

**Supervisor:** Supervisor; **Builder:** Coder; **Auditor:** Debugger
**Scope:** `ui/handlers/review_handler.py` + `tests/test_review_queues.py`.
Binding ruling: **D3 REV 4b** (complete enumeration) in
`docs/specs/phases/SPEC-10-PREFLIGHT-DECISIONS.md`.

---

## BUG #4 (MEDIUM) — reject_file + revert_file_to_sha take no project lock

Auditor probes (`scratch/dbg_sp2fix3_reject_paths.py` / `_reject_full.py`):
reject_changes ∥ reject_file → checkout overlap=2, errors ×7; ∥
revert_file_to_sha → ×5; reject_file ∥ reject_file → ×14 (`index.lock:
File exists`). Same class as BUG#1 — my REV 4a enumeration omitted the two
checkout paths.

### Fix (D3 REV 4b)

Wrap the `git_ops.checkout_paths(...)` call (and its immediate validation)
in BOTH `reject_file` and `revert_file_to_sha` with
`with self._project_lock_for(project_name):`. Same rules as round 3:
nesting project_lock→queue_lock only (these paths take no other lock);
idle callbacks fine; no GLib blocking inside.

## BUG #5 (LOW) — one unhardened daemon-bleed test

`test_accept_all_two_agents_no_concurrency`
(tests/test_review_queues.py ~:419) uses an inline `_recording_stage` with
no path filter. Any prior test's outliving `_do` daemon lands in its mock
(`scratch/dbg_sp2fix3_daemonbleed.py`: pair run 8/8 FAILED; alone passes;
default order hides it).

### Fix

Route it through `_make_counting_stage(..., only_path=<its own repo>)`
(or equivalent path filter). **Add the both-orders regression to the file**
— a small test or comment documenting that
`test_accept_all_two_agents_no_concurrency` must pass after any
daemon-spawning test (the auditor's pair shape).

### Tests (RED-first)

1. `test_reject_file_vs_reject_changes_no_race` — active session
   (checkpoint), two dirty files; `reject_file` ∥ `reject_changes`;
   instrumented `checkout_paths` pass-through counting max concurrency
   (path-filtered); assert max == 1, no `index.lock` in any emitted text,
   session resolves.
2. `test_reject_file_vs_reject_file_no_race` — two concurrent `reject_file`
   on different files, same repo: max checkout == 1, no index.lock. (RED
   today: ×14/20 overlap errors.)
3. `test_revert_file_vs_batch_accept_no_race` — `revert_file_to_sha` ∥
   `accept_agent_queue` (root entry): max mutating-git concurrency == 1.

RED proofs: auditor rates above are the pre-fix rates — reproduce at least
one shape RED yourself before the fix.

## Do NOT change

- Everything already locked (rounds 2–3) — untouched.
- `check_changes` (read-only diff — verified no lock needed).
- BUG C registered non-goal.

## Battery (paste all)

- xvfb 5-file suite, ruff multiset, pyright 0
- RED proofs
- Post-fix stress: the two reject shapes ≥10 iters, expect 0 errors /
  overlap=1
- Full-file test_review_queues.py run ×3 (order-stability, per BUG#5's
  both-orders concern)

## COMPLETENESS (mandatory)

- [ ] BUG#4: two locks added — diff hunks + RED (3 race tests)
- [ ] BUG#5: path-filter hardening + both-orders note — diff hunk
- [ ] Battery + stress + ×3 order-stability runs
- [ ] Related issues found, NOT fixed (flagged)

Invoke `prompts/steelFramedCodeWriter.md`. Please write and report when done.
