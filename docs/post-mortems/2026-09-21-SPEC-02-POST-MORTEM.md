# SPEC-02 Post-Mortem — Error Surfacing + Empty-Message Rollback

**Date:** 2026-09-21 · **Commit:** `f7925daa` · **Spec:** `docs/specs/SPEC-02-ERROR-SURFACING.md`
**Status:** COMPLETE (single-phase spec, both tasks landed together)

## What shipped

1. **Turn-fatal error feed cards** (`_do_error`): "Turn failed: <Agent>" system cards with
   provider/model/exception_type metadata from `_crabcakes_context`. Entire emission block
   (context read → construction → `add_card`) is best-effort-guarded — a feed failure logs
   and falls through to the lifecycle-end fire; the activity drawer can never stick on
   "running" because of card emission.
2. **Cancel is not a failure:** `CANCEL_MESSAGE = "Cancelled by user"` shared constant;
   all three cancel dispatch sites (idle `cancel()` :1044, mid-loop shutdown :1384, mid-loop
   user :1401) emit it; the handler's card block skips it. No failure cards for deliberate
   cancels — SPEC-09 stop-all's spray scenario pre-defused.
3. **Empty-assistant-message rollback** (`_run_loop` except path): trailing truly-empty
   assistant messages (no content, no tool_calls) are popped (≤5) before FAILED
   `_auto_save`, so failed turns never persist empty assistant entries. Strip is
   origin-agnostic; the turn's own user message shields pre-existing history; the three
   early terminal paths run no rollback (all verified claims, pinned by tests).

## Audit trail

- **Round 1 (5 findings, all ACCEPTED, all fixed):** test pollution of real
  `~/.config/crabcakes/conversations/` (HIGH — 4 files/run + `migrate_conversation_files()`
  rewrite; fixed via module-scoped XDG redirect); non-dict `_crabcakes_context` escaping
  `_do_error` past `_session_completed` (MED); raising `add_card` skipping lifecycle-end
  (MED); cancel-as-failure cards (MED); rollback comment claiming turn-attribution the
  positional strip doesn't have (doc).
- **Round 2 (4 findings):** #6 mid-loop short-form cancels still emitting failure cards
  (MED — fixed in micro-round; probe matrix 2×2); #7 cap-5 residue >5 empties persists 1
  (LOW — BANKED: needs >5 consecutive empties, unreachable config today); #8 isolation not
  self-falsifiable + body cap untested (LOW — fixed: sentinel + cap pin, falsifiers
  mutation-proven); #9 mislabeled test name (LOW — fixed: rename).
- **Micro-round 2:** #8a/#8b/#9 delivered; Coder banked a reusable lesson (module-import
  double-bind trap: no `tests/__init__.py`, so `import tests.test_x` binds a second module
  object and misses fixture writes).

## Falsifiability discipline (worked, keep)

Every fix-proving test was mutation-verified: 5/11 fail without round-1 fixes; round-2
mutations killed by their named tests (re-raise kill, skip-removal, strip-all-roles,
user-msg-add removal, cap raise); micro-round mutants (autouse-removed, slice-removed)
killed by sentinel + cap tests. Debugger's falsifiability matrix is now a standing
requirement for test-bearing rounds.

## Environment notes (2026-09-21 session)

- `/tmp/spec01venv2` is gone; `.venv/bin/python` (Python 3.12.3, pytest 9.1.1) is the
  working env. It was created by an in-app agent earlier today — creation + `pip install`
  spawned ~300 file-tree feed cards mid-session.
- **In-app OOM incident (4 kills, 18:52–19:25):** agent-launched pytest runs of
  `tests/test_agent_runtime.py` exploded to 30 GB anon-RSS inside the app cgroup; kernel
  OOM-killed the scope (looked like "app crashes"). Same selections pass in 0.9 s in a
  clean shell under a 4 GB cap — detonator is app-env-specific, still unidentified
  (candidates eliminated: env vars incl. `CRABCAKES_PROJECTS_DIR`/`GTK_MODULES`, venv
  diff, test content diff, specific `-k` selections). RULE until solved: never run bare
  `tests/test_agent_runtime.py` in-app; class selectors only.
- **Pre-existing test pollution (banked, NOT SPEC-02):** `TestTurnStateMachine` +
  `TestStreamErrorIntegration` (the sanctioned regression selectors!) leak 18 `rt*.json`
  fixtures into the real conversations dir per run. Same class of bug SPEC-02 round-1
  fixed for its own file. Needs a sweep + isolation fix + real-dir debris cleanup
  (500+ fixture files, ~608 counted at one point — includes earlier SPEC-01 delivery
  debris). NOTE: `ls | wc -l` totals are noisy during live sessions (`special:*.json`
  churn); count `rt*` only, bracket tightly.
- `ruff format` drift on runtime.py/ARH remains banked (no `[tool.ruff]` config; PM
  decision pending).

## Numbers

- Tests: 20 in `tests/test_error_surfacing.py` (11 original + 5 round-1 + 2 cancel-constant
  + 2 micro-round). Regression: SPEC-01 30 green; TurnStateMachine + StreamErrorIntegration
  35 green.
- Baselines held exactly: ruff runtime 26 / ARH 29 / tests 0; pyright runtime 18 / ARH 0;
  `ruff format --check` clean on the new test file.
- Commit: 4 files, +1061/−5.

## Next

**SPEC-03 (P11 memory ratchet)** — next in the binding chain, per the roadmap ordering
(P11 gates all multi-agent work). Banked items riding or queued: BUG #7 (cap residue),
`test_agent_runtime.py` pollution fix + OOM root-cause (candidate for P11 measurement
harness work), `:1283` pre-turn shutdown notice (SPEC-09 spray audit).
