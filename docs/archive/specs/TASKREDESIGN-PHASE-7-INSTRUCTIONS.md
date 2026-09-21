# Phase 7 — Workflow State (spec-planning rename + legacy row migration)

**Loop authority:** `prompts/implementationLoop.md` + `prompts/implementationSupervisor.md`
**Spec:** `docs/specs/SPEC-TASK-SYSTEM-FULL-REDESIGN.md` (READ §7.1 — authoritative)
**Builder playbook:** `prompts/steelFramedCodeWriter.md` — load fresh, Discovery block, follow every rule.

## Files to change (1)

1. **`utils/workflow_state.py`** — MODIFY. Change `PHASES[3]` from `task-planning` to `spec-planning`, update `PHASE_PROMPTS`, and add backward-compatible migration of old `task-planning` rows on read.

## Tests to update: `tests/test_workflow_state.py` (spec §10)

## Current state (verified — read the file yourself to confirm)

- `PHASES = ["onboarding", "discovery", "architecture", "task-planning", "implementation", "testing", "ship"]` (line ~30)
- `PHASE_PROMPTS["task-planning"] = "`prompts/cc-task-planning.md`"` (line ~45)
- `_PHASE_INDEX = {name: i for i, name in enumerate(PHASES)}`
- `_read_workflow_lines(project_path)` auto-migrates old 6-column format to 7-column on read (transparent, persisted). After that migration, the file is in 7-column format with `task-planning` as a phase name.
- `_parse_new_row(line)` parses a 7-column row → `(idx, name, prompt, status, started, completed, notes)`.
- `_make_phase_row(idx, name, status, started, completed, notes)` emits a 7-column row (looks up `PHASE_PROMPTS.get(name)` for the prompt column).
- `advance_phase(project_path, "onboarding")` marks onboarding done + sets the NEXT phase (index 1 = `discovery`) to current. Changing index 3's name does not affect the onboarding→discovery transition.

## Spec §7.1 — required changes

### `PHASES` rename

Change `PHASES[3]` from `"task-planning"` to `"spec-planning"`:
```python
PHASES = [
    "onboarding",
    "discovery",
    "architecture",
    "spec-planning",   # was "task-planning" (SPEC-TASK-SYSTEM-FULL-REDESIGN §7.1)
    "implementation",
    "testing",
    "ship",
]
```

### `PHASE_PROMPTS` update

```python
PHASE_PROMPTS = {
    "onboarding":     "`prompts/system/project-onboarding.md`",
    "discovery":      "`prompts/cc-discovery.md`",
    "architecture":   "`prompts/cc-architecture-design.md`",
    "spec-planning":  "`prompts/cc-spec-planning.md`",   # was task-planning → cc-task-planning.md
    "implementation": "`prompts/implementationLoop.md`",
    "testing":        "`prompts/steelFramedCodeWriter.md`",
    "ship":           "`prompts/cc-workflow-guide.md`",
}
```

### Legacy row migration (spec §7.1 — the tricky part)

Existing `workflow.md` files on disk contain `task-planning` as a phase name (index 3). On read, these must migrate to `spec-planning` PRESERVING status, started/completed dates, and notes — only the phase-name and prompt columns change.

Spec §7.1 gives the migration pattern. Add it in `_read_workflow_lines`, AFTER the existing old-format (6→7 column) migration, BEFORE returning the lines:

```python
# SPEC-TASK-SYSTEM-FULL-REDESIGN §7.1: migrate task-planning → spec-planning
migrated = False
for index, line in enumerate(lines):
    parsed = _parse_new_row(line)
    if parsed is None or parsed[1] != "task-planning":
        continue
    phase_idx, _name, _prompt, status, started, completed, notes = parsed
    lines[index] = _make_phase_row(
        phase_idx, "spec-planning", status, started, completed, notes
    )
    migrated = True
if migrated:
    _write_workflow_lines(project_path, lines)
```

Key points:
- Use the EXISTING row parser/emitter (`_parse_new_row` / `_make_phase_row`), NOT a broad string replacement — spacing may differ. `_make_phase_row` looks up `PHASE_PROMPTS.get("spec-planning")` so the prompt column is regenerated correctly.
- Preserve `status`, `started`, `completed`, `notes` — only rewrite the phase-name and prompt cells.
- Persist the migrated rows ONCE (don't re-migrate on every read — after migration, the row says `spec-planning`, so the `parsed[1] != "task-planning"` guard skips it).
- After writing, the function returns the migrated `lines` (the existing flow already does `return lines` at the end).

**Where exactly to add it:** in `_read_workflow_lines`, after the existing `if _is_old_format(lines):` block (which handles 6→7 column migration), add the task-planning→spec-planning migration block. The function should then `return lines` as before.

### Post-migration verification (spec §7.1)

After writing the migrated rows, the spec says: "re-read via `_read_workflow_lines()` and confirm all rows parse as valid seven-column rows; if any fail, log a warning." Add a lightweight check: after the migration write, loop the lines once more and `_parse_new_row` each; if any returns None for a line that looks like a phase row (starts with `| <digit>`), log a warning. Keep this defensive — don't crash.

### `advance_phase` behavior unchanged

`advance_phase(project_path, "onboarding")` still marks onboarding done and sets index 1 (`discovery`) to current. The only downstream effect of the rename: a project that has advanced through onboarding→discovery→architecture and then calls `advance_phase(project_path, "architecture")` will set index 3 to current — which is now `spec-planning` (was `task-planning`). This is correct.

## Tests (`tests/test_workflow_state.py`) — spec §10

Add tests:
- `PHASES` contains `spec-planning` (not `task-planning`).
- `PHASE_PROMPTS["spec-planning"]` == `` `prompts/cc-spec-planning.md` ``.
- `PHASE_PROMPTS` does NOT contain `task-planning` as a key.
- `_PHASE_INDEX["spec-planning"]` == 3.
- **Migration:** seed a workflow.md with a `task-planning` row (with status/started/completed/notes), call `_read_workflow_lines` (or any public function that reads), confirm the row is now `spec-planning` with status/dates/notes PRESERVED and the prompt column updated to `cc-spec-planning.md`.
- **Migration idempotence:** calling the read twice does NOT double-migrate (the row stays `spec-planning`).
- **Onboarding transition unchanged:** `advance_phase(project_path, "onboarding")` on a fresh project still sets `discovery` as current (not `spec-planning`).
- **Full lifecycle:** advance onboarding→done, discovery→done, architecture→done → `spec-planning` is now current.
- `init_workflow(project_path)` on a fresh project writes `spec-planning` (not `task-planning`) in the table.

Update any existing test that asserts `task-planning` is in PHASES — those must now assert `spec-planning`.

Run `python3 -m pytest tests/test_workflow_state.py -v` and paste full output. Also run `python3 -m pytest tests/test_project_awareness.py tests/test_workflow_state.py -q` to confirm no cross-module regression.

## Rules
- `prompts/steelFramedCodeWriter.md` — Discovery block first.
- Do NOT modify Phase 1-6 files.
- Verify after edit: `python3 -c "from utils.workflow_state import PHASES, PHASE_PROMPTS; assert 'spec-planning' in PHASES; assert 'task-planning' not in PHASES; print('OK')"`.
- Verify the migration preserves data: write a test that seeds status/dates/notes and asserts they survive.
- Keep the existing 6→7 column migration intact (the new migration runs AFTER it).

## COMPLETENESS checklist (mandatory)

```
COMPLETENESS:
- [x] Edit 1: PHASES[3] task-planning→spec-planning — evidence: <grep/python check>
- [x] Edit 2: PHASE_PROMPTS spec-planning→cc-spec-planning.md, task-planning key removed — evidence: <grep>
- [x] Edit 3: legacy row migration in _read_workflow_lines (preserves status/dates/notes) — evidence: <test name + output>
- [x] Edit 4: migration idempotent (no double-migrate) — evidence: <test name>
- [x] Edit 5: onboarding transition unchanged — evidence: <test name>
- [x] Edit 6: init_workflow writes spec-planning — evidence: <test name>
- [x] Edit 7: existing tests updated — evidence: <pytest output>
- [x] Edit 8: no Phase 1-6 files modified — evidence: git status
```

Report: diffs, grep/python checks, pytest output, COMPLETENESS block. Flag related issues, don't silently fix. Write when done.
