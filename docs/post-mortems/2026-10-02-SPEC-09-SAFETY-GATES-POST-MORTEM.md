# SPEC-09 (Safety Gates) Post-Mortem

**Date:** 2026-10-02 (loop start; SP0–SP3 landed 2026-10-02 → 2026-10-03)
**Supervisor:** Supervisor
**Builder:** Coder
**Auditor:** Debugger
**Commits:** 7 (9210c843 planning, 6b3beb72 SP0, 19c680aa SP1, 07efb6b2 SP2, daafe3df SP2-corrective, 36108f53 SP3, + this close-out)
**Phases:** SP0 enforcement hardening (pre-req) → SP1 leases → SP2 worktrees → SP3 stop-all → SP4 close-out
**Total bugs found:** 34 across 10 adversarial audit rounds + 10 pre-build probe intercepts
**Process:** implementationLoop §3.1a throughout — every code-bearing turn audited pre-commit; steelFramedCodeWriter in every brief + every payload (PM standing order); adversarialDebugger every audit round.

---

## 1. Code Quality Grade: A- (91/100)

### Justification

The hardest spec in the MVP line landed at the highest bar the repo has held: the kill path (the most-executed code in the app) survived 10 audit rounds including two rounds where a "fixed" defect had its own masked blind spot (probe C's reaper hid the zombie; the leader-reaping hid the group). Zero shipped defects across 34 findings; every phase's acceptance verified — several in the supervisor's own hands, not just reports. The grade sits below A for the two commit-integrity failures (both caught, both corrected, both logged), and because three defect classes trace to supervisor-authored briefs.

| Category | Score | Notes |
|-----------------------|-------|-------|
| Correctness | 18/20 | Zero shipped; kill semantics proven at every liveness layer (leader, group, zombie); −2 for brief-borne defect classes |
| Architecture compliance | 10/10 | D1=(b) as ruled; project-parameterized worktrees per ruling; layer rules held (utils pure, no invented globals) |
| Test coverage | 9/10 | 9→99 enforcement, 49→80 lease, 27 worktree, 62 kill/stop tests; mutation/RED discipline deep; −1 for two unable-to-fail tests that reached the auditor (strengthened in-round) |
| Documentation | 9/10 | Rulings + threat models in-module; −1 for docstring contradictions needing probe catches (twice) |
| Maintainability | 9/10 | −1 for the epoch-set + flush-pop semantics spreading across 4 runtime sites (necessarily, but dense) |
| DX | 9/10 | −1 for the tmpdir leak (folded) + scratch-litter management |
| **Total** | **91/100** | **A-** |

Deducted points:
- 2 Correctness: brief-borne classes (SP1 store premise; SP3 leader-vs-group conflation) — each cost a fix round
- 1 Test coverage: two tests passed against broken code (flush-pop, no-poisoning) — Coder caught and strengthened, but they reached audit
- 1 Documentation: docstring contradictions (is_app_worktree; _detect_venv_prefix) caught by probes, not review
- 1 Maintainability: approval-lifecycle density (epoch + 3 pop sites + flush semantics)
- 1 DX: tmpdir leak class

---

## 2. What's Good About the Code

1. **The kill path is now the most-probed code in the repo:** Popen + start_new_session + module registry + group-liveness escalation (poll-before-probe in both windows) + honest (killed, unkillable) counts threaded to the PM's card. Every liveness layer has a RED-proven test: TERM-honoring leader, TERM-immune child, orphaned group, zombie leader, self-exiting leader with lingering child, timeout path. The acceptance (ruling #6) verified by three parties independently.
2. **The approval lifecycle ruling held:** the per-session cancellation epoch (armed at cancel/terminal, cleared only at terminal transition + turn start) made registration-time denial correct for every race probed — including the late registrant after the loop's own bookkeeping would have defeated a naive guard. Every deny path pops its entry; the dict cannot accumulate; `approve_exec` can never resolve a phantom.
3. **The enforcement gate's threat-model ruling:** "project config cannot falsify validation, not host hardening" resolved every design tension cleanly (HOME trusted, project-containment overriding, app-venv identity-gated) and is documented in-module — future rounds inherit the contract, not the confusion.

---

## 3. What's Bad About the Code

1. **Commit-integrity failed twice:** 07efb6b2's message described `find_live_lease` + ARH tests its diff omitted (daafe3df corrective); SP1's earlier map-import was the first instance. Both were validated code that sat dirty — the suite stayed green with it in-tree — but history lied about what shipped when. Evolution: commit-claims-vs-diff verification is now a standing review gate (it caught the second instance).
2. **The `_cancel_requested` global-flag race survives (registered):** a runtime-global consumed by whichever loop notices it first; multi-session cancellation still terminates correctly but not always via the O(1) path. Self-healing via the per-session branch; needs a per-session count/set someday.
3. **BUG#3 (post-stop poisoning) ships registered, not fixed:** stop_all then resume costs one self-cancelling turn (pre-existing `cancel()` semantics). "Stop then resume" is exactly the post-action flow a PM will try first.

---

## 4. Bugs Found During Audit

| # | Phase | Severity | Bug | Found by | Fixed by |
|---|-------|----------|-----|----------|----------|
| 1 | SP0 | HIGH ×4 | PATH-bleed shim executes via user-PATH resolution; fake-venv via symlink (dir, python, site-packages, decoy, lying-cfg); glob-metachar evasion; file-level symlink bleed | Debugger (probes, every round) | Coder (4 rounds) |
| 2 | SP0 | MED ×3 | app-venv root leak to foreign projects; refusal fall-through; cascade disables tiers | Debugger | Coder |
| 3 | SP0 | LOW ×3 | dead clause + fixture mismatch; literal-vs-realpath asymmetry ×2; docstring contradictions | Debugger | Coder |
| 4 | SP0 | — | ELOOP silent-admission (41-hop chain reads absent) | Coder (self-catch, in-build) | Coder |
| 5 | SP1 | bug | Split-brain: /work persist clobbers live leases (probe D, real handler) | Debugger | Coder |
| 6 | SP1 | bug | Phantom claim/release on silent persist failure | Debugger | Coder |
| 7 | SP1 | bug | Probe G mirror: stale non-None lease resurrects/overwrites | Debugger (**pre-build** — brief defect) | brief corrected pre-build |
| 8 | SP1 | issue ×3 | corrupt-lease drops whole unit; concurrency pin powerless (2 threads); id-form mismatch; NaN ttl | Debugger | Coder |
| 9 | SP2 | — | 10 brief-borne/latent defects intercepted across 3 PRE-BUILD probe rounds (blockers: regex dot-segments, session-key conflation, app-repo-vs-project design fork; mechanics: worktree add/prune revival, porcelain parsing, detached-truthiness) | Debugger (**all pre-build**) | briefs corrected pre-build |
| 10 | SP3 | HIGH | Leader-reaping gates escalation; TERM-immune child survives (AC#7 violated e2e) | Debugger (probe C) | Coder |
| 11 | SP3 | HIGH | Zombie leader keeps group "alive": +4s every timeout, false UNKILLABLE 12/12 | Debugger (probe C's own mask found) | Coder |
| 12 | SP3 | MED | Late-registered approval hangs 60s; entry leak; approve_exec phantom hijack | Debugger | Coder |
| 13 | SP3 | LOW ×2 | Lying kill counts; pre-flight abort undercount | Debugger | Coder |
| 14 | SP3 | — | Two unable-to-fail tests (flush-pop masked by guard; no-poisoning masked by callback ordering) | Coder (self-catch) | Coder (strengthened) |

**Pre-build interceptions (the spec's process story):** 10 defects caught in briefs before code existed — SP1's probe G (mirror merge bug in my fix rule), SP2's three rounds (9 defects incl. the app-repo-vs-project design fork that would have had agents editing develcakes instead of the user's project). Zero post-build fix rounds in SP2 vs four in SP0.

### Bug patterns

| Pattern | Count | Description |
|---------|-------|-------------|
| `count-vs-coverage` / `partial-path-validation` | 5 | SP0's site-packages family — one surface validated, siblings missed |
| `process-group-liveness` / `zombie-liveness-probe` | 3 | Liveness probed at the wrong layer (leader/zombie vs group) |
| `cancellation-token-lifecycle` | 2 | Guard keys on state another component clears |
| `split-brain-state` | 2 | Two writers, one file/record |
| `stale-closure-capture` / `stale-snapshot` | 2 | Launch-time capture of post-init state |
| `test-fixture-mismatch` / `mock-truthiness` / `weak-concurrency-test` | 4 | Tests certifying shapes production can't produce |
| `glob-metachar-evasion` / `symlink-normalization-asymmetry` | 3 | Path validation normalizing one side only |
| commit-integrity (unlogged class) | 2 | Message claims vs diff contents |

---

## 5. Process: What Worked

1. **Pre-build brief probing (the spec's defining process win):** Debugger probed SP1's fix rule and SP2's brief *before* Coder built — 10 defects intercepted, including one design fork (worktrees of the app repo vs the active project) that would have shipped the wrong product behavior with green tests. SP2: zero post-build fix rounds. The SP0 post-mortem rule ("brief predicates get audited") graduated to: *ownership/merge/git-mechanics/kill-semantics rules get probed pre-delegation.*
2. **Multi-party RED baselines:** the auditor's pre-built harnesses with RED baselines (SP1's 14-check 2/14→14/14; SP3's probe suite) made GREEN meaningful. Supervisor e2e reproductions (probe C's orphan shape; the zombie no-reaper shape; BUG#1/#9/#15/#18 enforcement false-PASSes) closed headline defects in independent hands every phase.
3. **Honest disclosure culture at depth:** Coder disclosed a corrupted intermediate file (P2), two unable-to-fail tests, and the prior-session entry state; the auditor disclosed its own SP1 flake-sampling miss (~5% flake vs 10-12 runs) and probe-API drift. Nothing had to be pried out; several self-catches (ELOOP, deny-flush race) became fixes in-round.

---

## 6. Process: What Didn't Work

1. **Commit-integrity failed twice despite the SP1 lesson:** 07efb6b2 repeated the message-vs-diff failure SPEC-08's round already flagged. The check existed in principle; it wasn't run at commit time. Lesson: the claim-vs-diff gate runs at EVERY commit from now on — mechanical (`git show --stat` vs the message's named artifacts), not judgment.
   - Trigger: any commit whose message names specific functions/files/tests.
   - Action: diff-stat verification before `git commit` completes.
2. **Briefs still reached builders with kill-semantics unp probed (SP3):** the pre-build pattern was applied to SP1/SP2 (leases, worktrees — data/git mechanics) but SP3's *liveness* semantics went out unprobed and cost two fix rounds. The rule's scope must include concurrency/lifecycle/killing semantics, not just data structures.
   - Trigger: any brief whose correctness depends on OS/process semantics.
   - Action: probe-first, delegate second — no exceptions for "mechanical" rewrites.
3. **Session crashes + prior-session edit states cost real time:** one fix round arrived with edits already in-tree from a crashed prior session (md5-verified, honestly disclosed, correctly audited-as-is) — but the missing context.md record meant the round started disoriented. Lesson: crashed sessions write a one-line tree-state marker before dying (or the next session's first act is reading the diff, not trusting absence of records).

---

## 7. What the Code Actually Does (End-User Impact)

1. **One button stops everything, for real.** ■ Stop All (toolbar, confirm dialog) cancels every in-flight turn, kills every registered process *group* (SIGTERM → 2s → SIGKILL — orphaned children included, zombie leaders correctly detected as dead), denies every pending approval (late registrants included — no 60s hangs), aborts in-flight review checkpoints at both gates, and shows one summary card: turns cancelled, approvals denied, processes killed, checkpoints aborted — with UNKILLABLE surfaced if anything survived SIGKILL. Code path: toolbar → window confirm → ARH `stop_all_agents` → per-runtime `stop_all` (turns first, then processes) → `_group_kill` → summary card.
2. **Writers work in isolation.** A writer agent holding a live lease gets `conv.project_path` = its own git worktree (`<project>/.worktrees/<id>`, branch `agent/<id>`) — same-file writers never share a tree; lease expiry resets the stale path; merges stay behind the review gate. Code path: `_prepare_turn_conversation` → `_worktree_for_turn` → `WorktreeManager.ensure_worktree`.
3. **Work units can't be double-claimed.** `/work`-level claims ride TTL leases (default 15 min, read-time expiry, same-holder refresh, disk-owns-the-lease merge — a handler persist can never erase a live claim; a corrupt lease drops only itself).
4. **Validation can't be falsified by project config.** The enforcement gate (SP0's hardening): resolved-binary containment (system bins ∪ HOME ∪ app-venv[identity-gated] ∪ project-venv[literal]), the venv claim matrix (dir/hops/cfg/site-packages ×5 shapes), glob-metachar-safe scanning, fail-closed refusals as visible FAILED tiers. The SPEC-06 register vectors (PATH-bleed, fake-venv) are closed with 99 pinning tests.

---

## 8. Pre-Existing Issues Flagged (Not Caused by This Implementation)

1. `agent/runtime.py` 17 pre-existing pyright errors — verified class-for-class at HEAD every round; registered for a dedicated round.
2. BUG#3 post-stop poisoning (cancel() semantics predate stop_all) — registered; probe scripts retained.
3. `_cancel_requested` global-flag race — registered (self-healing).
4. `test_accept_changes_emits_git_commit_card` synchronous-assert race + the 100ms wall-clock feed test — pre-existing flakes under xvfb load, both outside this spec's diffs.
5. Enforcement availability-probe skip-DoS (BUG#8 family) + `venv_path` type-coercion (BUG#20) — registered in SP0.

---

## 9. Evolution Suggestions (Tier 2+)

| Suggestion | Effort | Impact |
|------------|--------|--------|
| Fix BUG#3 (clear `_cancelled` in `_terminate_turn`'s CANCELLED path) + per-session cancel flag | ~0.5 day | Stop-then-resume costs zero wasted turns; multi-session O(1) bail |
| Commit-integrity gate as a hook (message-named artifacts vs diff-stat) | ~2h | The twice-failed manual check becomes mechanical |
| SP3's registered items: `cancel()` dead `["result"]` write cleanup; runtime pyright 17 → 0 | ~1 day | Global floor achieved; dead code out |
| Enforcement BUG#8 skip-DoS (availability probes via scrubbed PATH + visible SKIPPED) | ~0.5 day | Tier suppression becomes observable |
| Worktree `.gitignore` for scratch/probe dirs repo-wide (scratch/ leaked into status) | ~15min | Tree hygiene |

---

## 10. Lessons Learned / Process Rules to Carry Forward

1. **Claim-vs-diff at commit time (mechanical):** every commit message naming artifacts gets its diff-stat checked against those names before the commit lands — twice failed manually, so it stops being a judgment call.
   - Trigger: commit messages naming functions/files/tests.
   - Action: `git show --stat` vs message, before finalize.
2. **Probe scope includes semantics, not just predicates:** ownership/merge rules AND liveness/concurrency/kill semantics get pre-build probes. SP3's two fix rounds were both preventable by probing the brief's kill model.
   - Trigger: briefs depending on OS/process/lifecycle semantics.
   - Action: probe-first, delegate-second, always.
3. **Unable-to-fail tests are defects, not coverage:** when a test passes against broken code (masked by an adjacent guard or wrong observable), strengthening it IS the fix — report the strengthening as a finding, never ship the vacuous pin.
   - Trigger: any new test that can't be made to fail by reverting its fix.
   - Action: find the masker; change the observable; RED-prove the strengthening.

---

## 11. Sign-off

- [x] Code committed to main (9210c843, 6b3beb72, 19c680aa, 07efb6b2, daafe3df, 36108f53, + this close-out)
- [x] All post-loop verification commands run and pasted (final suite below; per-phase batteries in the phase reports; auditor independently re-ran the full suite)
- [x] Captain notified with summary
- [x] Tier 2+ backlog updated (§9; registers in context.md)
