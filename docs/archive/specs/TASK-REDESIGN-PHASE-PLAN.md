# Task System Full Redesign — Phase Plan & Tracking

**Supervisor:** Supervisor (special:supervisor)
**Builder:** Coder (special:coder)
**Auditor:** Debugger (special:debugger)
**Spec:** `docs/specs/SPEC-TASK-SYSTEM-FULL-REDESIGN.md`
**Baseline commit:** 9a71afe (clean, 3317 tests collected)
**Loop authority:** `prompts/implementationLoop.md` + `prompts/implementationSupervisor.md`

## How this loop runs

- One phase at a time. Coder implements per `prompts/steelFramedCodeWriter.md`.
- After every code-bearing turn, the work is handed to Debugger to run
  `prompts/adversarialDebugger.md` (11 sections) on the files in scope.
- Supervisor runs independent verification (tests, greps, diff inspection) after
  each phase returns clean from the auditor.
- Phase is signed off only after BOTH the auditor is clean AND independent
  verification passes.
- Post-mortem (§6 format of implementationLoop.md) is written at the end.

## Phase inventory

| # | Phase | Files (new/modified) | Status | Audits | Bugs |
|---|-------|----------------------|--------|--------|------|
| 1 | WorkUnit model + store + __init__ + task.py deprecation | `models/work_unit.py` (N), `models/__init__.py`, `models/task.py` | ✅ | 2 | BUG#1 from_dict membership; BUG#8/#9 test gaps |
| 2 | Work persistence + migration | `utils/work_persistence.py` (N) | ✅ | 2 | BUG#1/#2/#3 crash-bugs; BUG#9 sibling on re-audit |
| 3 | Work Handler (commands, no handoff) | `ui/handlers/work_handler.py` (N) | ✅ | 3 | BUG#12 done invariants; BUG#13/#14 missing-authz; BUG#16/#18 assign consistency |
| 4 | Command registration (no `aliases=`) | `ui/handlers/command_handler.py`, `ui/window.py` (BUG#20 fix) | ✅ | 1 | BUG#20 partial-completion (window.py wiring done by Supervisor); BUG#21 test gap |
| 5 | Window wiring + project lifecycle | `ui/window.py`, `ui/handlers/command_handler.py` | ✅ | 0 | clean (task_handler→work_handler rename + load_for_project/close_project wiring) |
| 6 | Awareness integration | `utils/project_awareness.py` | ✅ | 1 | BUG#25 build_awareness_dict missing work counts (pre-existing gap closed via project-bound loader) |
| 7 | Workflow state (spec-planning + migration) | `utils/workflow_state.py` | ✅ | 0 | clean (BUG#1 parser-pipe-in-cell pre-existing, deferred to Tier 2+; BUG#2 weak-assertion fixed by Supervisor) |
| 8 | Prompts (cc-spec-planning, cc-task-planning redirect, cc-workflow-guide, crabcakes-commands) | prompts | ✅ | 0 | clean (docs phase; static-content tests verify no stale task-planning) |
| 9 | ARCHITECTURE.md updates | `docs/ARCHITECTURE.md` | ✅ | 0 | clean (docs phase; all 9 §14 areas updated, line counts verified) |
| 10 | Final verification + pattern sweeps + post-mortem | — | ✅ | 0 | post-mortem written; 3507 tests collected; all §12 acceptance criteria + §16 pattern sweeps pass |

## Verification gates (run by Supervisor after every phase)

- `python3 -m pytest tests/test_<module>.py -v` for new/changed modules
- `grep` for removed patterns (counts must be 0 unless migration-only)
- `git diff --stat` to confirm every file in scope was touched
- Full `python3 -m pytest -q` at phase 5 and phase 10 (catches regressions)
