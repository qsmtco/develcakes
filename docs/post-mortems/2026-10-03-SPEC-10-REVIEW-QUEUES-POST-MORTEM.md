# SPEC-10 (Review Queues + Batch Accept) Post-Mortem

**Date:** 2026-10-03 (loop start; SP1–SP4 landed same day)
**Supervisor:** Supervisor; **Builder:** Coder; **Auditor:** Debugger
**Commits:** 5 (106cb739 SP1, 31802bea SP2, c256c433 SP2b, 209483bd SP3, + this close-out)
**Phases:** SP1 git_ops trailer → SP2 handler queues + batch accept → SP2b ARH turn-complete
checkpoints → SP3 bar UI + feed bridge → SP4 close-out
**Total bugs found:** 22 across 8 adversarial audit rounds + 1 pre-build probe round
**Process:** implementationLoop §3.1a throughout — every code-bearing turn audited pre-commit;
steelFramedCodeWriter in every brief + payload (PM standing order); adversarialDebugger every
round; pre-build probing applied to the decisions doc (SPEC-09's defining lesson).

---

## 1. Code Quality Grade: A- (92/100)

### Justification

The spec's central trap — "accept commits every pending checkpoint" — was caught by the
auditor's pre-build probe before a line of handler code existed (D2 checkpoints leave trees
CLEAN; a naive accept-commit livelocks), and the fix-round arc drove the concurrency model
through four refinement rulings (D3 REV 2→REV 4b). Every finding landed RED-honest; three
vacuous-test shapes were self-caught by the builder and rebuilt; the auditor's closure
verification used mutation-neutering and AST containment, not report-trusting. The grade
sits below A for the enumeration failures: the lock ruling needed THREE corrections
(REV 4 → 4a → 4b) — each gap was the supervisor's brief under-scoping the auditor-corrected
ruling — and one build round shipped four timing-masked negative tests.

| Category              | Score | Notes |
|-----------------------|-------|-------|
| Correctness           | 18/20 | Zero shipped defects; every race shape proven 0-fail at 95+ patient iterations; −2 for the three-round lock-enumeration arc |
| Architecture compliance | 10/10 | Layers held (view pure, handler no-GTK-dialog, utils pure); no fabricated APIs (the brief's warning block worked); ARH/ReviewHandler seams clean |
| Test coverage         | 9/10 | 87 new tests, all RED-first; guard-deletion mutation proofs throughout; −1 for the four timing-masked negatives that reached audit |
| Documentation         | 9/10 | Rulings D1–D8d in-decision-doc with rationale; in-code comments cite their rulings; −1 for the one docstring enumeration error (BUG#3 SP1) |
| Maintainability       | 9/10 | −1 for lock-spread density (project lock × 6 sites + per-session lock + queue lock — necessarily dense, all documented) |
| DX                    | 9/10 | −1 for the ruff-baseline confusion the supervisor introduced in the SP1 brief (measured 0 without running it) |
| **Total**             | **92/100** | **A-** |

Deducted points:
- 2 Correctness: brief-borne under-scoping of REV 4 (twice) — each cost a fix round
- 1 Test coverage: four negative tests passed with guards deleted (timing-masked)
- 1 Documentation: SP1 docstring boundary-enumeration error
- 1 Maintainability: three-lock discipline density
- 1 DX: stale ruff/pyright baselines in the supervisor's SP1/SP2b briefs

---

## 2. What's Good About the Code

1. **The pre-build probe paid for the whole spec:** Debugger's probe of the *decisions doc*
   found BUG#3–#6 (COMPLETED-only dispatch, accept-livelock, wrong liveness predicate,
   threading ambiguity) before code existed — D3's "mark-reviewed bookkeeping" semantics,
   the load-bearing design choice of the spec, came from that probe, not from the original
   spec text. Zero post-build fix rounds were spent discovering design; every round refined.
2. **The lock model ended provably complete:** the auditor's AST enumeration walk of every
   `git_ops.*` call site (0 mutating calls outside a project lock) plus runtime nesting
   probes (checkpoint→project, root-branch-only) turned "we think it's serialized" into a
   machine-checkable invariant — and the same walk caught the supervisor's two enumeration
   omissions (BUG#1 round 3, BUG#4 round 4).
3. **RED-honesty as a living discipline:** three vacuous tests were caught and rebuilt
   in-round (the seed test's first shape, the diff-read-error test's patch-race mask, the
   serialization test's root-shape pre-locking), and the auditor's mutation-neutering
   verification (delete the guard → the test MUST fail) is now the suite's admission
   standard. The joint two-layer stale-token RED (single-inner deletion documented as
   unreachable-by-test) is the honest way to pin a defense-in-depth property.

---

## 3. What's Bad About the Code

1. **The rulings needed four revisions (REV 2→4b):** the accept-serialization ruling was
   correct in wording ("ALL entry points") but the supervisor's phase instructions scoped
   the edits narrowly twice, costing rounds 3 and 4. Quantification: 2 extra fix rounds,
   ~4 auditor probe suites.
   - Evolution: rulings that define an *enumeration* ship with the auditor's AST walk as
     part of the FIRST fix round, not as a later audit probe.
2. **In-memory queues lose nothing but visibility on restart (accepted GAP-5b trade):** the
   worktrees/branches survive, but the PM's pending view does not — a restart mid-review
   leaves agent branches unqueued until the next checkpoint sweep.
   - Evolution: persist queue heads (project, agent, last-sha) in feed-prefs-style JSON;
     rehydrate on project open (~1 day).
3. **Shared-root `-A` cross-attribution (registered BUG C, twice-confirmed):** a root-tree
   checkpoint or accept can sweep another writer's uncommitted files under the wrong
   `Agent:` trailer. Bounded by D2's checkpoint-per-turn + superset review, but exact
   root attribution needs per-file staging.
   - Evolution: scope agent accept-staging to the entry's file set (SP2's queue entries
     don't carry file lists today; the checkpoint sha does — `diff --name-only` at accept).

---

## 4. Bugs Found During Audit

| # | Phase | Severity | Bug | Found by | Fixed by |
|---|-------|----------|-----|----------|----------|
| 1 | pre-build | HIGH ×3 + MED | Accept-livelock on clean trees; is_worktree_of wrong predicate; COMPLETED-only dispatch unspecified; threading ambiguity (D2/D3/D8b rewritten pre-code) | Debugger (pre-build probe) | Supervisor (decisions REV 2) |
| 2 | SP1 | MED | 8/10 splitlines boundaries forge trailer lines | Debugger | Coder (1 round) |
| 3 | SP1 | LOW | non-str trailer raises outside try: | Debugger | Coder |
| 4 | SP1 | LOW | docstring enumeration inaccuracies | Debugger (re-audit) | Supervisor (doc-only) |
| 5 | SP2 | HIGH | clean-tree root accept strands (PARTIAL accepted-0) | Debugger | Coder (REV 3) |
| 6 | SP2 | HIGH | accept_all_queues per-agent threads race (strand 6/10) | Debugger | Coder (REV 4) |
| 7 | SP2 | MED ×3 | mock-truthiness tests; pm queue never drained; accept commit trailer-less | Debugger | Coder (REV 2/3 + real-git tests) |
| 8 | SP2 r2 | HIGH | cross-invocation accept stampede (accept∥accept_changes index.lock ×13-17) | Debugger | Coder (project lock, REV 4) |
| 9 | SP2 r3 | MED + LOW ×2 | three unlocked git sites (enumeration gap); stale-card in-lock emit; lock-dict leak (ruled no-eviction) | Debugger | Coder + Supervisor ruling |
| 10 | SP2 r4 | MED + LOW | reject_file/revert_file_to_sha unlocked (second enumeration gap); daemon-bleed test instance | Debugger | Coder (REV 4b) |
| 11 | SP2b | MED | four timing-masked gate tests (guards deletable green) | Debugger | Coder (not-_wait pins) |
| 12 | SP2b | LOW-MED | same-session overlapping worktree checkpoints race (18/25 lost entries) | Debugger | Coder (D8d per-session lock, 0/25) |
| 13 | SP3 | MED | bar built over surviving queues renders chipless (GAP-5b reachable) | Debugger | Coder (seed at build) |
| 14 | SP3 | LOW | stale-drop card missing metadata["agent"]; pm-only no-op button | Debugger | Coder |

Compounding: none — every defect was caught in its own phase before the next phase built on
it. The two enumeration gaps (9, 10) were the same defect class caught twice, each closed
within one round.

### Bug patterns

| Pattern | Count | Description |
|---------|-------|-------------|
| `clean-tree-assumption` / `partial-sanitization` | 3 | Logic written against dirty-tree/fixed-char-set premises the data invalidates |
| `race-condition` (stampede/index.lock) | 4 | Unserialized concurrent mutators on one repo — closed by the REV 4b enumeration |
| `mock-truthiness` / `timing-masked-test` | 5 | Tests that pass with the guard deleted (sync asserts racing daemons; always-success mocks) |
| `unbounded-growth` / missing-consumer | 2 | pm queue enqueued with no drain; lock-dict eviction question |
| `wrong-predicate` | 2 | Path-shape checks used where liveness/registration was needed |

---

## 5. Process: What Worked

1. **Pre-build probing of the decisions doc (again):** SPEC-09's defining lesson, applied
   from the first turn — the auditor probed D2/D3/D8b *semantics* against real code before
   SP2 existed. The accept-livelock (the spec's central flaw) never reached code.
2. **Mutation-neutering as the closure standard:** every audit closure was verified by
   deleting the fix and requiring the test to fail — which surfaced the four timing-masked
   tests, the vacuous seed test, and the two-layer stale-token property honestly.
3. **Ruling-versioning in one place:** D1–D8d in the pre-flight doc, with each REV citing
   its audit finding — the auditor, builder, and supervisor all quoted the same ruling text
   across eight rounds with zero drift on intent (scope drifted twice; intent never).

---

## 6. Process: What Didn't Work

1. **The supervisor's briefs under-scoped the supervisor's own rulings (twice):** REV 4 said
   "ALL entry points"; the round-2 instruction edited one function; round 3's instruction
   still missed two checkout paths. Two fix rounds spent on instruction-scope, not design.
   - Lesson: when a ruling defines an enumeration, the instruction lists the enumeration
     *exhaustively* (grep-derived), and the first fix round includes the auditor's
     call-site walk.
2. **Stale baselines in briefs:** SP1's brief asserted "ruff 0 today" (reality: 22
   pre-existing) and SP2b's cited a pyright baseline belonging to a different file — both
   corrected by the builder measuring reality, both disclosed. Cheap to prevent: measure
   baselines at brief-writing time, never from memory.
   - Lesson: every baseline number in a brief comes from a command run that day.

---

## 7. What the Code Actually Does (End-User Impact)

1. **Every agent's work now arrives attributed and reviewable per agent.** When a writer
   agent finishes a turn under an active review session, its work is checkpoint-committed
   (worktree or root) with an `Agent: <session_key>` trailer and appears as a chip on the
   review bar (`coder (3)`). Code path: turn dispatch snapshot → `_do_response_complete` →
   `_maybe_agent_checkpoint` (daemon thread, stop-all-gated) → `enqueue_agent_checkpoint`
   → bar refresh.
2. **The PM can accept one agent's queue or everyone's in one confirmed action.** Accept
   All (agent)/(everyone) opens a dialog showing exactly N; OK runs the serialized batch —
   worktree items marked reviewed (their commits already carry trailers), root items
   committed with the trailer; failures abort remaining with a partial-completion card;
   stale worktrees drop with error cards and never poison the batch. The pm queue is never
   batch-touched — the PM's own /accept drains it. Code path: bar buttons → confirm →
   `_accept_agent_queue_sync` (project lock) → per-item outcomes → summary card.
3. **Nothing git-mutating races anything else anymore.** Double-clicks, concurrent accepts,
   PM /accept during a batch, per-file rejects mid-accept — all serialize per project
   (AST-verified: zero mutating git calls outside the lock), same-session checkpoints
   serialize per session (0/25 lost entries, was 18/25), and the full suite (4378) is green.

---

## 8. Pre-Existing Issues Flagged (Not Caused by This Implementation)

1. `utils/git_ops.py` 22 ruff findings at HEAD (BLE001 ×18 = the file's documented
   never-raise design; I001/UP045/PLW1510/SIM102) — verified pre-existing at 92694f2f;
   repo-level baseline decision registered (SPEC-08/09 carried the same class).
2. `file_log`'s `\x1f` subject guard shares the fixed-set fragility class (NUL-separator
   format makes it low-risk) — SP1 audit flag, registered.
3. Shared-root `-A` staging cross-attribution (BUG C) — pre-existing accept semantics,
   twice-confirmed, bounded by superset review; evolution item.
4. `test_accept_changes_emits_git_commit_card` synchronous-assert flake under xvfb load —
   SPEC-09-registered daemon-thread race, untouched by this spec, standalone-green ×2 here.
5. `check_changes` torn-diff display under concurrent accept — display-cosmetic
   (auditor's hard-race probe: 0 parse failures), registered.

---

## 9. Evolution Suggestions (Tier 2+)

| Suggestion | Effort | Impact |
|------------|--------|--------|
| Queue persistence + rehydrate on project open (GAP-5b trade closed) | ~1 day | Restart no longer hides pending agent work |
| Per-file accept staging from checkpoint sha (`diff --name-only`) | ~0.5 day | Closes BUG C: exact root attribution |
| `git_ops.NOTHING_TO_COMMIT` shared constant (exact-string match ×2 modules) | ~15 min | Removes the duplicated-literal fragility the auditor flagged |
| set_queue_view incremental diffing (no full rebuild) | ~2h | O(1) churn per mutation at large rosters |
| Single-lock atomic queue snapshot for refresh (torn chip counts) | ~30 min | Cosmetic consistency under concurrent accept |

---

## 10. Lessons Learned / Process Rules to Carry Forward

1. **Enumeration rulings ship with an exhaustive grep-list in the instruction:**
   - Trigger: any ruling of the form "every X must Y".
   - Action: the instruction lists every site (grep-derived at writing time); the first
     fix round includes the auditor's call-site walk (AST if parseable).
2. **Baselines are measured, never remembered:**
   - Trigger: writing any verification gate into a brief.
   - Action: run the command that day; paste the number's provenance (two stale baselines
     slipped through this loop — both caught by the builder, both disclosed).
3. **Negative tests pin by absence-window, not synchronous zero:**
   - Trigger: any test asserting something did NOT happen when the something is
     thread-deferred.
   - Action: `assert not _wait(lambda: <it happened>, timeout=…)` — a sync `== 0` races
     the daemon and passes with the guard deleted (four such shipped this loop).

---

## 11. Sign-off

- [x] Code committed to main (106cb739, 31802bea, c256c433, 209483bd, + this close-out)
- [x] All post-loop verification commands run and pasted (full suite 4378 passed / 3
      skipped in 825.04s; per-phase batteries in the phase reports; AC#1 trailer e2e
      re-verified at close-out)
- [x] Captain notified with summary
- [x] Tier 2+ backlog updated (§9; registers in context.md)
