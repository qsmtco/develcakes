# PHASE 6 of 7 — seed prompts on project create

**Spec:** `docs/specs/SPEC-PROJECT-PROMPTS-DIRECTORY.md` (§2.5; data flow §3.1)
**Scope:** exactly 1 file — `ui/handlers/project_handler.py`. Nothing else.

## Change

In `create_project()` (~line 141), the flow currently is:

```
makedirs(path) → init_project_config(...) → _auto_add_onboarding_agents(path)
→ try: init_workflow(path) except Exception: pass
→ init_repo(path) → stage_all → commit → awareness snapshot
```

Insert the seed BETWEEN the `init_workflow` try/except block and the `init_repo` call — so the seeded `.crabcakes/prompts/` is included in the project's initial git commit (matches spec §3.1 ordering):

```python
        # SPEC-PROJECT-PROMPTS-DIRECTORY §2.5: seed the per-project prompt
        # library (.crabcakes/prompts/) so new projects start with the app's
        # user-facing prompt set. Copy-only-if-missing + idempotent; failure
        # is non-fatal (logged) — same policy as init_workflow above.
        try:
            seed_project_prompts(path)
        except Exception as e:
            _logger.warning("prompts seed failed for %s: %s", path, e)
```

Import: find how `init_workflow` is imported in this file (check the actual imports — it may be `utils.workflow_state`, not project_awareness). Add `seed_project_prompts` to whichever module provides it correctly — it lives in `utils/project_awareness.py`. If project_awareness is already imported here, extend that import; otherwise add `from utils.project_awareness import seed_project_prompts`.

Note: `seed_project_prompts` itself never raises (all OSError paths return False internally) — the try/except here is defense-in-depth matching the init_workflow pattern, not a requirement of the callee. Keep it anyway (consistency).

## Verification constraints

No dedicated unit seam exists for create_project (it drives git + filesystem). Evidence required:
- Paste `grep -n "seed_project_prompts\|init_workflow\|init_repo" ui/handlers/project_handler.py`.
- If exec available: `python3 -B -m pytest tests/test_project_awareness.py tests/test_seed_project_prompts.py -q -p no:cacheprovider` (regression — nothing should change).
- If exec gated: say so explicitly; paste syntax-check result from your edit tool.

## Rules

- Use the **steelFramedCodeWriter** prompt at `prompts/steelFramedCodeWriter.md`.
- Read `create_project` fully (~lines 141–210) plus the imports before editing.
- Flag ANY deviation explicitly.
- ALSO: clean up your scratch files from the repo root if exec permits — delete `find_bytes.py`, `find_offsets.py`, `verify_window.py`, `window_check.py` (they were Phase 5 workarounds; supervisor confirmed they are untracked debris). If you cannot delete, list them in your report and the supervisor will remove them in the exec window.

COMPLETENESS:
- [x/not done] Edit 1: seed call inserted between init_workflow and init_repo — evidence (grep)
- [x/not done] Import added/extended correctly — evidence
- [x/not done] Scratch files deleted OR listed for supervisor cleanup
