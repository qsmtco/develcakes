# SPEC-11 (Rename Divergence + Config Migration) Post-Mortem

**Date:** 2026-10-04
**Supervisor:** Supervisor; **Builder:** Coder; **Auditor:** Debugger
**Commits:** 4 (7b2f7122 SP1, f0e8f65e SP2, 8cf0740b SP3, + this close-out)
**Phases:** SP1 migration helper → SP2 env divergence → SP3 app identity + wiring → SP4 close-out
**Total bugs found:** 24 across 7 audit rounds (4 SP1 + 1 SP2 + 2 SP3) + 1 pre-build decision review
**Process:** implementationLoop §3.1a throughout; steelFramedCodeWriter every build; adversarialDebugger every round.

---

## 1. Code Quality Grade: A- (91/100)

### Justification

The migration — a filesystem routine whose failure mode is *silently stranding the one
user's data* — was driven through four fix rounds that found seven distinct data-loss or
data-corruption shapes (flat-dir silent loss, straggler-rollback poison, unreadable-source
silent success, symlinked-subdir silent loss, DAG false-positive, destination-reentry
runaway, marker poison). Every one was RED-proven and closure-verified; the final round
verified the icon fix against the *real GTK4 icon theme* rather than a string match. The
grade sits below A because the first build shipped a CRITICAL that the builder's own test
seeder fabricated away (always seeding nested subdirs, never the real flat v1 shape) — a
test-design failure that a naive suite would have certified as done — and because the
supervisor's briefs carried two stale/under-counted facts (7 sites was 9; a baseline number
from the wrong file).

| Category | Score | Notes |
|---|---|---|
| Correctness | 19/20 | Zero shipped defects; every data-loss shape closed and independently reproduced; −1 for the CRITICAL that reached audit |
| Architecture compliance | 10/10 | Layers held; BLOCKING-1 honored (state-dir untouched); utils pure; D3 ordering verified end-to-end |
| Test coverage | 9/10 | 58 new tests across 3 files; mutation matrices (20 mutants) with honest survivors; −1 for the seeder-mask that let the CRITICAL ship green |
| Documentation | 8/10 | Rulings + deferral recorded; −2 for the README over-sweep (3 regressions incl. a LIVE env row) and the 7 stale layout rows |
| Maintainability | 9/10 | −1 for the migration's dense fail-closed guard stack (necessarily, all documented) |
| DX | 9/10 | −1 for brief-borne fact drift (site count, baselines) |
| **Total** | **91/100** | **A-** |

Deducted points:
- 1 Correctness: CRITICAL flat-dir bug reached audit (seeder masked it)
- 1 Test coverage: the same seeder-mask class
- 2 Documentation: README over-sweep regressions + pre-existing stale layout rows
- 1 Maintainability: guard-stack density
- 1 DX: two brief-borne fact errors

---

## 2. What's Good About the Code

1. **The migration is genuinely fail-closed across the whole failure surface:**
   unreadable source → named failed report, never the marker; symlink cycles →
   chain-scoped detection (diamonds/chains pass, true cycles fail); destination reentry →
   bounded rejection (0.0s, was a 94.8s runaway); marker write → atomic tmp+replace with
   cleanup. `utils/config.py:82-330` — the one-shot either fully succeeds or reports
   exactly what failed, and the v1 directory is never touched.
2. **D3 ordering is structurally verified, not asserted:** the banner path was driven
   through the *real* `main()` (success/failure/no-op/emit-once/no-feed/add_card-raises),
   the import graph was traced to prove nothing creates the config dir before migration,
   and the ordering pin matches the exact statement (`report = migrate_v1_config()`), with
   reorder-and-delete both RED-proven. `main.py:365`.
3. **The env-divergence fallback is centralized and symmetric:** one `get_env` helper
   (`utils/config.py:44`) with presence-not-truthiness semantics, all 9 read sites routed,
   and the MIGRATE_STORE writer gated so the documented old-name kill-switch survives —
   the fix that closed the HIGH rename-fallback-defeat.

---

## 3. What's Bad About the Code

1. **The v1 shape was tested wrong before it was tested right:** the SP1 seeder fabricated
   a `nested/` subdir in every functional dir, so the real flat `conversations/` layout was
   never exercised — the CRITICAL `FileNotFoundError` shipped green. Quantification: one
   full audit round + a CRITICAL fix round.
   - Evolution: seeders must mirror production shapes (the auditor's standing rule; now
     codified — flat-default, nested-variant).
2. **The rename sweep over-reached into documentation:** a blanket `crabcakes` →
   `develcakes` pass rewrote `.crabcakes/` state-dir mentions (BLOCKING-1 violation,
   self-caught), deleted a LIVE `STT_MODEL_SIZE` env row, made the migration sentence
   self-referential, and renamed nonexistent layout files. Quantification: 5 README/doc
   findings across SP3.
   - Evolution: doc sweeps should be term-scoped with an allowlist (`.crabcakes/`,
     history files, v1-source paths), not global replace.
3. **The layout block carried pre-existing staleness from SPEC-04/05** (7 rows naming
   deleted files/dirs: gateway client, chat parser/renderer/segments, KB server/lookup,
   auxilium wizard) — surfaced only because the auditor's probe standard is
   "layout matches the tree".
   - Evolution: a repo-tree-vs-README-layout test would catch this class mechanically.

---

## 4. Bugs Found During Audit

| # | Phase | Severity | Bug | Found by | Fixed by |
|---|-------|----------|-----|----------|----------|
| 1 | SP1 | CRITICAL | flat v1 `conversations/` (real shape) fails every copy — seeder masked it | Debugger | Coder (r1) |
| 2 | SP1 | HIGH | partial dir survives rollback → poisons guard-3 forever | Debugger | Coder (r1) |
| 3 | SP1 | HIGH | unreadable v1 dir = silent success + marker (data stranded) | Debugger | Coder (r1) |
| 4 | SP1 | MED ×4 | empty-dir guard presence-not-content; verify wrong reference; dangling-symlink write-through; 4+ vacuous drift tests | Debugger | Coder (r1) |
| 5 | SP1 r2 | HIGH | symlinked subdir silently skipped + marker written | Debugger | Coder (r2) |
| 6 | SP1 r2 | MED ×2 | empty-dir-at-file permanent loop; verify zero coverage (false docstring) | Debugger | Coder (r2) |
| 7 | SP1 r3 | HIGH | global-seen cycle guard false-positives legitimate DAGs (diamond/chain) | Debugger | Coder (r3) |
| 8 | SP1 r3 | MED | failed report labels PRIOR successes; pseudo-name fallback | Debugger | Coder (r3) |
| 9 | SP1 r4 | MED | destination-reentry runaway (94.8s, ~600 dirs) | Debugger | Coder (r4) |
| 10 | SP1 r4 | LOW-MED | marker write-after-create poisons the one-shot | Debugger | Coder (r4) |
| 11 | SP1 r4 | LOW ×2 | marker-tmp symlink write-through; marker-tmp FIFO hang | Debugger | registered (SP4 carry) |
| 12 | SP2 | HIGH | MIGRATE_STORE setdefault defeats old-name kill-switch | Debugger | Coder |
| 13 | SP2 | LOW | ACTIVE_PROJECT_PATH writer/reader asymmetry (stale root) | Debugger | Supervisor (§6) |
| 14 | SP3 | MED ×2 | icons can't resolve theme name; wiring pin asserts a comment | Debugger | Coder |
| 15 | SP3 | LOW ×3 | README over-sweep (live env row deleted; self-referential sentence; nonexistent filenames) | Debugger | Coder |
| 16 | SP3 r | LOW ×2 | 2 stale Auxilium layout rows; missing WM-class comment | Debugger | Supervisor (§6) |

Compounding: none across phases — each phase's defects were closed before the next built
on it. The SP1 arc alone took four rounds, but every round found a *new* distinct shape
rather than a regression of a prior fix.

### Bug patterns

| Pattern | Count | Description |
|---------|-------|-------------|
| `data-loss-on-failure-path` | 5 | A failure shape that silently strands/misroutes the one-shot migration |
| `test-seeder-mask` | 3 | Fixtures fabricating a shape production never produces |
| `guard-false-positive/negative` | 2 | Cycle guard too broad (DAG) / too narrow (dest reentry) |
| `fallback-defeat` | 2 | A rename where writer and reader disagree about the fallback |
| `doc-over-sweep` | 5 | Global term replace reaching history/state-dir/live rows |

---

## 5. Process: What Worked

1. **The supervisor's independent reproduction on every headline finding:** the CRITICAL
   flat-dir, the empty-dir guard, the unreadable-v1 marker, the diamond DAG, the
   destination reentry, and the kill-switch defeat were each reproduced in the
   supervisor's own probe before routing — turning "the auditor says" into "I saw it."
2. **The pre-build decision review + BLOCKING-1:** reading the spec's premise against the
   *live* environment ("v1 is the build host and reads `.crabcakes/`") caught the
   state-dir-rename hazard before any code — the single most important call of the loop.
   The deferred sweep list is recorded for the post-MVP unit.
3. **Escalating the two "pre-existing" failure-path gaps (round 4):** the auditor
   recommended deferring the reentry runaway and marker poison to SP4; the supervisor
   overrode on risk (both strand user data — the migration's core contract) and fixed them
   in-loop. Both closed with RED proofs.

---

## 6. Process: What Didn't Work

1. **The builder's mutation "CAUGHT" claim was contaminated (SP1 r1):** a mutation rerun
   ran while a test was failing in the plain suite, so every mutant returned rc=1
   regardless — a false "all caught." The supervisor's zero-coverage probe caught it; the
   builder disclosed it honestly in round 2.
   - Lesson: a mutation harness must assert a GREEN baseline before interpreting any
     mutant result; "all caught" is meaningless if the suite was already red.
2. **Two supervisor briefs carried wrong facts:** "7 sites" (reality 9 — both extras were
   in the supervisor's own GAP-1 text) and a pyright baseline belonging to a different
   file. Both were caught by the builder measuring reality.
   - Lesson (carried from SPEC-10): brief facts are measured, never recalled; enumerations
     are grep-derived at writing time.

---

## 7. What the Code Actually Does (End-User Impact)

1. **A v1 user's first develcakes launch migrates their world.** `migrate_v1_config()`
   copies `~/.config/crabcakes/` (agents, providers, config, conversations, audit log,
   transcript DB + sidecars) to `~/.config/develcakes/`, byte-verifies each file, writes
   the `MIGRATED_FROM_V1` marker, and surfaces one banner card: "Config migrated from v1 —
   N copied." Failures name the entry and say "v1 untouched; develcakes starts fresh."
   The v1 directory is never modified. Code path: `main()` → `migrate_v1_config()` →
   `_pending_migration_report` → `on_activate` banner.
2. **The app is now `develcakes` everywhere:** launcher `develcakes`, app id
   `com.develcakes.app`, window/taskbar icon `develcakes` (wheel-verified theme
   resolution), desktop entry installed. The env-var family is `DEVELCAKES_*`, and every
   variable still answers to its legacy `CRABCAKES_*` name for one release — including the
   documented `MIGRATE_STORE=0` kill-switch, which works under either name.
3. **Nothing that ran as v1 breaks:** the per-project `.crabcakes/` state dir keeps its
   name (BLOCKING-1), so a v1 install and a develcakes install can coexist on one repo
   without fighting over project state.

---

## 8. Pre-Existing Issues Flagged (Not Caused by This Implementation)

1. Marker-tmp islink/isreg parity gap (symlink write-through + FIFO hang on a
   user-planted `MIGRATED_FROM_V1.tmp`) — registered SP4 carry (threat = user pre-plants
   hostile state in their own config dir; no legitimate install reaches it).
2. `agent/runtime.py:110` comment says "setdefault" for the now-gated write — cosmetic,
   SP4 sweep.
3. README layout carried 7 stale rows from SPEC-04/05 deletions — fixed in SP3 (mechanical);
   a tree-vs-layout test would prevent recurrence.
4. StartupWMClass may not match GTK4's WM class for `com.develcakes.app` — flagged in the
   desktop file; post-MVP polish.
5. The reinstall pulled the declared-but-absent STT stack (faster-whisper/ctranslate2/
   numpy) into the venv — venv weight only (lazy import); the post-MVP STT-removal unit
   must prune pyproject deps.

---

## 9. Evolution Suggestions (Tier 2+)

| Suggestion | Effort | Impact |
|------------|--------|--------|
| Marker-tmp islink/isreg parity (BUG#6/7) | ~30 min | Closes the last unguarded-open shapes |
| Repo-tree-vs-README-layout test | ~2h | Catches stale doc rows mechanically |
| Post-MVP `.crabcakes/`→`.develcakes/` rename + sweep (recorded list) | ~1 day | Completes the divergence |
| Remove `CRABCAKES_*` fallback + sweep the 7 test files setting old names | ~0.5 day | Ends the one-release window |
| Prune STT deps from pyproject with the STT-removal unit | bundled | Drops faster-whisper/ctranslate2/numpy |

---

## 10. Lessons Learned / Process Rules to Carry Forward

1. **Test seeders mirror production shapes, never convenient ones:**
   - Trigger: any fixture that constructs an input tree/dir.
   - Action: seed the REAL shape (flat v1 dirs); add a variant for the alternate; a
     fabricated shape is a false-negative generator (this loop's CRITICAL).
2. **A mutation "all-caught" claim requires a proven-green baseline in the same run:**
   - Trigger: any mutation-matrix result.
   - Action: assert the unmutated suite is green immediately before interpreting mutants;
     a red baseline makes every mutant falsely "caught."
3. **Doc sweeps are term-scoped with an explicit allowlist:**
   - Trigger: any global rename/replace across docs.
   - Action: list the terms that must NOT be swept (state-dir names, history files, v1
     source paths, live env vars) before the pass; grep-verify each afterwards.

---

## 11. Sign-off

- [x] Code committed to main (7b2f7122, f0e8f65e, 8cf0740b, + this close-out)
- [x] All post-loop verification commands run and pasted (full suite below; per-phase
      batteries in the phase reports; AC#1 trailer-equivalent e2e for the migration)
- [x] Captain notified with summary
- [x] Tier 2+ backlog updated (§9; registers in context.md)