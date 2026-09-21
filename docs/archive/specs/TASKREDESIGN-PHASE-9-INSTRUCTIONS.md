# Phase 9 — ARCHITECTURE.md Updates

**Loop authority:** `prompts/implementationLoop.md` + `prompts/implementationSupervisor.md`
**Spec:** `docs/specs/SPEC-TASK-SYSTEM-FULL-REDESIGN.md` (READ §14 — ARCHITECTURE.md Updates Required — authoritative)
**Builder playbook:** `prompts/steelFramedCodeWriter.md` — load fresh, Discovery block, Rule 3 (verify every claim against source), Rule 8 (modify only what's in scope).

## Files to change (1)

1. **`docs/ARCHITECTURE.md`** — MODIFY. Update all sections covering the task→work-unit redesign per spec §14.

## No new tests

ARCHITECTURE.md is documentation. No test file needed (the prompt static-content tests from Phase 8 cover the prompts; ARCHITECTURE.md has no executable assertions). Verify via grep that stale references are updated.

## Spec §14 — sections to update

Read ARCHITECTURE.md in full first (or at least the sections below). Then update:

### 1. Directory tree (around lines 70-134)
- The `models/` tree comment listing exports: add `WorkUnit`, `WorkUnitStore`, `work_store` alongside (or replacing) `Task`, `TaskStore`, `task_store`. Note `task_store` is kept for backward compat.
- Add `work_unit.py` to the `models/` tree: `work_unit.py  # WorkUnit + WorkUnitStore + status/priority labels (replaces task.py as primary)`.
- Mark `task.py` as deprecated in the tree comment.
- Add `work_persistence.py` to the `utils/` tree: `work_persistence.py  # .crabcakes/work.json source of truth + generated tasks.md + legacy migration`.
- In `ui/handlers/`: replace `task_handler.py` line with `work_handler.py  # WorkHandler — /work commands + Supervisor handoff (replaces task_handler.py)`. Note task_handler.py is kept but deprecated.

### 2. Models table (around line 320)
- Update the `Task, TaskStore` row: note they are deprecated, superseded by `WorkUnit, WorkUnitStore`.
- Add a row for `WorkUnit, WorkUnitStore` → `work_unit.py` — Work Unit data model + in-memory store (primary); spec_path, supervisor/builder/auditor assignments, dependency graph, lifecycle statuses.

### 3. §3.3d `models/task.py` section (around line 384)
- Add a deprecation notice at the top of the section: task.py is superseded by work_unit.py; kept for import compatibility.
- Add a NEW section §3.3d1 (or §3.3d-prime) `models/work_unit.py — Work Unit Data Model` documenting: the WorkUnit dataclass fields, WorkUnitStore API, WORK_STATUSES/WORK_PRIORITIES, serialization (to_dict/from_dict with type validation), _work_init_counter, _validate_dependencies. Mirror the style of the existing §3.3d.

### 4. §3.21d `ui/handlers/task_handler.py` section (around line 1445)
- Add a deprecation notice: TaskHandler is superseded by WorkHandler in production wiring; kept for import compatibility.
- Add a NEW section documenting `ui/handlers/work_handler.py — Work Handler` covering: the constructor (injected deps), the `/work` command grammar + legacy aliases, `/work start` validation + Supervisor handoff, the status transition table, authorization (PM/Supervisor), path containment, persistence. Mirror the style of existing handler sections (e.g., §3.21 command_handler).

### 5. Persistence section (around line 3060)
- The line mentioning `TaskStore` import for awareness: update to reference `WorkUnitStore` / the project-bound loader (`_work_units_for_awareness`).
- Add `utils/work_persistence.py` to the persistence modules: owns `.crabcakes/work.json` (source of truth) and generated `tasks.md`; legacy migration; atomic writes; best-effort error handling.

### 6. Tests section (around line 3873)
- Update `tests/test_tasks.py` line: note it tests the deprecated TaskStore (still passes).
- Add: `tests/test_work_unit.py`, `tests/test_work_persistence.py`, `tests/test_work_handler.py`, `tests/test_task_redesign_prompts.py`.

### 7. Summary tree (around line 4137)
- Update `task.py` line to note deprecation + line count.
- Add `work_unit.py` and `work_persistence.py` and `work_handler.py` with line counts (run `wc -l` to get exact numbers).

### 8. Workflow / phase references
- Any mention of `task-planning` as a phase → `spec-planning`.
- Any mention of `cc-task-planning.md` as the planning prompt → `cc-spec-planning.md` (with note that cc-task-planning.md is a redirect).
- Any mention of an autonomous implementation engine → "manual `/work start #N` triggers the implementation loop (Supervisor loads implementationLoop.md)".

### 9. Awareness section
- Update the awareness description to reflect Work Unit counts (`total, spec_pending, spec_ready, in_progress, done`) instead of Task counts (`total, in_progress, blocked, pending, done`).

## Rules
- `prompts/steelFramedCodeWriter.md` — Discovery block first. Read the relevant ARCHITECTURE.md sections before editing.
- **Rule 3 (verify every claim):** run `wc -l` on the new files to get accurate line counts for the tree. Run `grep` to confirm method/class names you document actually exist.
- **Rule 8 (no collateral edits):** change ONLY the task→work-unit-related content. Do not reformat adjacent sections, do not "improve" unrelated docs.
- Do NOT modify Phase 1-8 files.
- ASCII tree formatting: continuation lines MUST use the same indentation pattern as surrounding entries (per steelFramedCodeWriter Step 5).
- Files must end with a trailing newline.

## Verification (run these and paste output)

```bash
# Stale references that should be gone or contextualized
grep -n "task-planning" docs/ARCHITECTURE.md   # should appear only in migration/compat context
grep -n "cc-task-planning" docs/ARCHITECTURE.md  # should reference the redirect, not as the active prompt

# New references that should be present
grep -n "work_unit.py\|WorkUnit\|work_store\|work_persistence\|work_handler.py\|WorkHandler\|spec-planning\|cc-spec-planning\|/work start" docs/ARCHITECTURE.md

# Line counts for the tree
wc -l models/work_unit.py utils/work_persistence.py ui/handlers/work_handler.py
```

## COMPLETENESS checklist (mandatory)

```
COMPLETENESS:
- [x] Edit 1: directory tree updated (work_unit.py, work_persistence.py, work_handler.py added; task.py/task_handler.py marked deprecated) — evidence: <grep>
- [x] Edit 2: models table updated (WorkUnit/WorkUnitStore row; Task/TaskStore deprecated) — evidence: <grep>
- [x] Edit 3: §3.3d task.py deprecation notice + new work_unit.py section — evidence: <grep>
- [x] Edit 4: §3.21d task_handler.py deprecation + new work_handler.py section — evidence: <grep>
- [x] Edit 5: persistence section updated (work_persistence.py, WorkUnitStore loader) — evidence: <grep>
- [x] Edit 6: tests section updated (new test files) — evidence: <grep>
- [x] Edit 7: summary tree updated (line counts via wc -l) — evidence: <wc output>
- [x] Edit 8: workflow/phase references (spec-planning, cc-spec-planning, /work start) — evidence: <grep>
- [x] Edit 9: awareness section (new count schema) — evidence: <grep>
- [x] Edit 10: no Phase 1-8 files modified — evidence: git status
```

Report: diffs, grep outputs, wc output, COMPLETENESS block. Flag related issues, don't silently fix. Write when done.
