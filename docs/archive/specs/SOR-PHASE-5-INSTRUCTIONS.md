# Phase 5 of 8 — Workflow onboarding completion hook for manifest cleanup

**Master spec:** `docs/specs/SPEC-SUPERVISOR-ONBOARDING-REFINEMENTS.md` §2.9 + §2.10.

## CRITICAL SCOPING DECISION (read carefully)

The master spec §2.9 mentions changing `PHASES` from `task-planning` to `spec-planning` and mapping it to `cc-spec-planning.md`. **DO NOT DO THIS.** Per the spec's own "Spec Sequencing" note (§2.9 + §1): the task-system redesign (`docs/specs/SPEC-TASK-SYSTEM-FULL-REDESIGN.md`) is **NOT being implemented in this loop**. Therefore this phase MUST **retain `task-planning` / `prompts/cc-task-planning.md`** exactly as-is.

**The ONLY change in this phase is adding the manifest-cleanup hook at onboarding completion.** Do not touch `PHASES`, `PHASE_PROMPTS`, `_PHASE_INDEX`, `_make_phase_row`, or any prompt mapping. Do not add a migration for `task-planning` → `spec-planning`.

## Goal

Wire `clean_manifest_skeleton(project_path)` (added in Phase 4) into `advance_phase()` so that completing the onboarding phase cleans comment-only sections from `.crabcakes/project.md`. Cleanup failures must be non-fatal and must not block the workflow transition.

## Rules
- Use the `prompts/steelFramedCodeWriter.md` prompt. Read `utils/workflow_state.py` in FULL before editing.
- Anchor edits to identifiers, not line numbers.
- Verify every claim with evidence (paste command output).

## Edit 1 — `utils/workflow_state.py`: add cleanup hook in `advance_phase`

In `advance_phase(project_path, phase_name)` (search for `def advance_phase`), add the manifest cleanup hook. Per master spec §2.9:

- Call `clean_manifest_skeleton(project_path)` ONLY when `phase_name == "onboarding"`.
- The cleanup must be **non-fatal**: wrap it in try/except so a cleanup I/O failure does not block the workflow completion. On failure, log or safely absorb per the helper's contract.
- Preserve `ValueError` for invalid phase names (the validation at the top of `advance_phase` must still raise before any cleanup or write happens — do NOT move it).
- Guarantee that a successful onboarding transition attempts cleanup exactly once.

**Recommended placement:** after the phase-name validation (`if phase_name not in _PHASE_INDEX: raise ValueError(...)`) and after the workflow file is written (`_write_workflow_lines(...)`), OR immediately before the write — your choice, but the cleanup must run only for the onboarding phase and must not block the transition. The cleanest pattern is to do it AFTER the workflow write succeeds, so a workflow-transition failure doesn't trigger a premature cleanup:

```python
    _write_workflow_lines(project_path, lines)

    # SOR §2.9: on onboarding completion, clean comment-only manifest sections.
    # Lazy import to keep workflow_state's module-level imports stable.
    # Non-fatal: a cleanup failure must not undo the workflow transition.
    if phase_name == "onboarding":
        try:
            from utils.project_awareness import clean_manifest_skeleton
            clean_manifest_skeleton(project_path)
        except Exception:
            import logging
            logging.getLogger(__name__).debug(
                "clean_manifest_skeleton failed for %s; workflow transition unaffected",
                project_path,
                exc_info=True,
            )
```

(Note: `workflow_state.py` already imports `get_crabcakes_dir` from `utils.project_awareness` at module top — confirm this. The lazy import of `clean_manifest_skeleton` inside the function avoids any import-cycle risk and keeps the hook localized. Use a module-level logger if one exists, or `logging.getLogger(__name__)` as shown.)

Confirm the existing module imports — if there's already a module-level `logger` or `_logger`, use it. If not, the inline `logging.getLogger(__name__)` is fine (check the file's style).

## Edit 2 — Tests in `tests/test_workflow_state.py` (NEW FILE — create it)

There is currently NO `tests/test_workflow_state.py` (verify with `ls tests/`). Create it. Read an existing simple test file (e.g. `tests/test_project_awareness.py` header/imports) for style. Required tests (spec §2.10):

1. `test_init_workflow_creates_file` — `init_workflow(project_path)` creates `.crabcakes/workflow.md` with all 7 phases; onboarding is `🔄 current`, others `⏳ pending`. (Basic smoke test of the module — establishes the workflow state for the other tests.)
2. `test_advance_phase_marks_done_and_advances` — after `advance_phase(project_path, "onboarding")`, the onboarding row is `✅ done` and the next phase (discovery) is `🔄 current`.
3. `test_advance_phase_invalid_name_raises` — `advance_phase(project_path, "bogus")` raises `ValueError`. (Regression guard — validation preserved.)
4. `test_onboarding_completion_invokes_manifest_cleanup` — Create a project with a skeleton manifest (`generate_project_skeleton` → all comment-only sections). Call `advance_phase(project_path, "onboarding")`. Assert that `.crabcakes/project.md` now has ONLY the title line (all comment-only sections removed) — i.e. the cleanup hook fired. Assert the workflow transition ALSO succeeded (onboarding row is `✅ done`).
5. `test_non_onboarding_phase_does_not_clean_manifest` — Advance a different phase (e.g. first init, then `advance_phase(project_path, "discovery")` after onboarding is done — or use a phase that doesn't require prerequisites for the test). Assert the skeleton manifest is UNCHANGED (cleanup hook only fires for onboarding).
6. `test_cleanup_failure_does_not_block_workflow_transition` — Monkeypatch `clean_manifest_skeleton` (in the `utils.project_awareness` module) to raise an exception. Call `advance_phase(project_path, "onboarding")`. Assert: (a) no exception propagates, (b) the workflow transition still happened (onboarding row `✅ done`), (c) the manifest is unchanged (cleanup failed). This is the critical non-fatal guarantee.

Use `tmp_path` for each test (isolated temp project dir). Each test must create `.crabcakes/` via `init_project_config` or `_ensure_crabcakes_dir` + `generate_project_skeleton` as needed. Match the fixture style of other test files. Add a module docstring explaining the file tests workflow state transitions + the onboarding cleanup hook.

## Verification (run and paste output)

```bash
# PHASES unchanged (task-planning retained, NOT spec-planning)
grep -n '"task-planning"\|"spec-planning"' utils/workflow_state.py
# must show task-planning present, spec-planning ABSENT

# PHASE_PROMPTS unchanged
grep -n "cc-task-planning\|cc-spec-planning" utils/workflow_state.py
# must show cc-task-planning present, cc-spec-planning ABSENT

# Cleanup hook present and onboarding-gated
grep -n "clean_manifest_skeleton\|phase_name == \"onboarding\"" utils/workflow_state.py

# Tests
XDG_CONFIG_HOME=/tmp/cctest_home/.config python3 -m pytest tests/test_workflow_state.py -q 2>&1 | tail -10
```

## COMPLETENESS (mandatory)

```
COMPLETENESS:
- [ ] Edit 1: clean_manifest_skeleton hooked into advance_phase, onboarding-only, non-fatal — evidence: grep outputs (hook present, onboarding-gated)
- [ ] Edit 1: PHASES + PHASE_PROMPTS UNCHANGED (task-planning retained) — evidence: grep showing task-planning + cc-task-planning present, spec-planning absent
- [ ] Edit 1: ValueError for invalid phase preserved — evidence: test 3 passes
- [ ] Edit 2: test_workflow_state.py created (6 tests) — evidence: pytest output
- [ ] Cleanup-failure non-fatal guarantee tested — evidence: test 6 passes
- [ ] Any related issue found, not silently fixed (report here)
```
