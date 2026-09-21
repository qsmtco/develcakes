# Phase 6 — Awareness Integration (Work Unit counts)

**Loop authority:** `prompts/implementationLoop.md` + `prompts/implementationSupervisor.md`
**Spec:** `docs/specs/SPEC-TASK-SYSTEM-FULL-REDESIGN.md` (READ §6 — Awareness Integration — authoritative)
**Builder playbook:** `prompts/steelFramedCodeWriter.md` — load fresh, Discovery block, follow every rule.

## Files to change (1)

1. **`utils/project_awareness.py`** — MODIFY. Replace `_get_task_info` to consume Work Units instead of `TaskStore`, update `build_awareness_block`'s Tasks line, update parameter names/docs.

## Tests to update: `tests/test_project_awareness.py` (spec §10)

## Current state (verified)

- `_get_task_info(task_store)` at line 732 returns `{total, in_progress, blocked, pending, done}` from `TaskStore.list_all()` matching legacy statuses (`in_progress`, `blocked`, `pending`, `done`).
- `build_awareness_snapshot(project_path, task_store=None)` at line 652 calls `_get_task_info(task_store)` and stores it under the `"tasks"` key.
- `build_awareness_block` at line 808 emits: `Tasks: {in_progress} in progress, {blocked} blocked, {pending} pending, {done} done`.
- `build_awareness_dict` does NOT emit a tasks line (only `build_awareness_block` does).
- Callers pass `task_store` positionally or by name; `build_awareness_snapshot(project_path)` (no store) is the common call, yielding all-zero counts today.

## Spec §6.1 — required changes

### `_get_task_info` replacement

Change `_get_task_info` to consume Work Units. The new schema (spec §6.1 — exact):

```python
{
    "total": int,
    "spec_pending": int,
    "spec_ready": int,
    "in_progress": int,
    "done": int,
}
```

Counts come from Work Unit statuses (`draft`, `spec-pending`, `spec-ready`, `in-progress`, `auditing`, `done`, `cancelled`). Mapping (spec §6.1 — must be explicit and tested):
- `total` = all units (regardless of status — including cancelled? NO: spec §6.1 says "cancelled and blocked compatibility states are not counted as active work" — but `total` should reflect all units. Use `len(all_units)` for total; the individual buckets exclude cancelled.)
- `spec_pending` = units with status `draft` OR `spec-pending` (spec §6.1: "draft may be included in spec_pending for awareness purposes, but the mapping must be explicit and tested").
- `spec_ready` = units with status `spec-ready`.
- `in_progress` = units with status `in-progress` OR `auditing`.
- `done` = units with status `done`.
- `cancelled` units are NOT counted in any bucket except `total`.

### Parameter rename

Rename the `task_store` parameter to `work_store` (or `work_units`) in `build_awareness_snapshot` and `build_awareness_block`. Update docstrings. The TYPE_CHECKING import of `TaskStore` should change to `WorkUnitStore` (or a generic store protocol). Keep the parameter optional (`=None`) and preserve backward compatibility for callers that pass positionally — the parameter position must stay the same.

**Caller compatibility:** grep for `build_awareness_snapshot(` and `build_awareness_block(` across the codebase. If any caller passes `task_store=` by keyword, update those call sites too. Most callers pass `project_path` only (no store) — those need no change.

### `build_awareness_block` Tasks line (spec §6.1)

Replace the stale line (808-814):
```python
f"Tasks: {tasks.get('in_progress', 0)} in progress, "
f"{tasks.get('blocked', 0)} blocked, "
f"{tasks.get('pending', 0)} pending, "
f"{tasks.get('done', 0)} done"
```
with the new schema (spec §6.1: "update the state-line builder to use spec_pending, spec_ready, in_progress, and done; remove references to pending and blocked"):
```python
f"Work: {tasks.get('in_progress', 0)} in progress, "
f"{tasks.get('spec_ready', 0)} spec-ready, "
f"{tasks.get('spec_pending', 0)} spec-pending, "
f"{tasks.get('done', 0)} done"
```
Keep the `if tasks.get("total", 0) > 0:` guard. The label changes from "Tasks:" to "Work:" to reflect the new model (spec §6.1: "build_awareness_block() must likewise display the new names and must not emit stale authoritative task fields").

### `build_awareness_dict` (spec §6.1)

`build_awareness_dict` does NOT currently emit a tasks line. Spec §6.1 says it "must display the new names and must not continue emitting stale pending/blocked fields." Since it doesn't emit task fields today, no change is needed to `build_awareness_dict`'s output — but verify it doesn't read the stale schema anywhere. (It calls `build_awareness_snapshot(project_path)` without a store, so its snapshot will have all-zero counts — that's the existing behavior and is fine.)

### `awareness.json` schema migration

Existing `awareness.json` files on disk have the old schema (`{total, in_progress, blocked, pending, done}`). The new schema replaces `blocked`/`pending` with `spec_pending`/`spec_ready`. This is fine — `awareness.json` is regenerated on each `save_awareness_snapshot` call, so stale fields simply age out. Do NOT write a migration for `awareness.json`; it's a generated snapshot, not a source of truth.

## Spec §6.2 — persistence timing (already satisfied)

"Load Work Units on project open before the first awareness snapshot." Phase 5 wired `work_handler.load_for_project(path)` into the project-open callback. The awareness builder reads from the work store, which is populated by `load_for_project`. No additional change needed in Phase 6 — just verify the builder doesn't write snapshots as a side effect (it doesn't — `build_awareness_dict`/`build_awareness_block` are read-only).

## How to get the work units in `_get_task_info`

The cleanest approach: pass the global `work_store` (from `models`) into `build_awareness_snapshot` the way `task_store` was passed before. Update the type hint to `WorkUnitStore | None`. `_get_task_info(work_store)` then calls `work_store.list_all()` and counts by Work Unit status.

**Important:** `build_awareness_snapshot` is called from many places (grep first). Changing the parameter NAME from `task_store` to `work_store` is safe if you also update keyword callers. Changing the parameter POSITION or making it required would break callers — keep it optional and in the same position (second arg).

Callers to check (grep `build_awareness_snapshot` and `build_awareness_block`):
- `window.py` or handler code that passes a store
- `init_project_config` (calls `build_awareness_snapshot(project_path)` with no store — fine)
- tests

## Tests (`tests/test_project_awareness.py`) — spec §10

Add tests for the new schema:
- `_get_task_info` with a WorkUnitStore containing units in various statuses → correct `{total, spec_pending, spec_ready, in_progress, done}`. Cover: draft counts in spec_pending; spec-pending counts in spec_pending; spec-ready counts in spec_ready; in-progress AND auditing both count in in_progress; done counts in done; cancelled counts ONLY in total (not in any bucket).
- `_get_task_info(None)` → all zeros.
- `build_awareness_block` output: when total > 0, the line says "Work:" with the new field names (spec_ready, spec_pending) and does NOT contain "blocked" or "pending" as field labels.
- `build_awareness_block` output: when total == 0, no Work/Tasks line at all.
- `build_awareness_snapshot` returns the new schema under the "tasks" key.

Update any existing test that asserts the old schema (`{total, in_progress, blocked, pending, done}`) — those assertions will fail and must be updated to the new keys.

Run `python3 -m pytest tests/test_project_awareness.py tests/test_work_unit.py tests/test_work_handler.py -v` and paste full output.

## Rules
- `prompts/steelFramedCodeWriter.md` — Discovery block first.
- Do NOT modify Phase 1-5 files (work_unit.py, work_persistence.py, work_handler.py, command_handler.py, window.py).
- Verify no stale field names: `grep -n "blocked\|pending" utils/project_awareness.py` — the words may appear in comments/context.md handling (unrelated), but NOT in the tasks-schema or the Work: line.
- Keep the parameter optional and in the same position.
- Run the FULL `tests/test_project_awareness.py` to catch any existing test that asserts the old schema.

## COMPLETENESS checklist (mandatory)

```
COMPLETENESS:
- [x] Edit 1: _get_task_info consumes WorkUnitStore, returns new schema — evidence: <test names + output>
- [x] Edit 2: build_awareness_block Work: line uses new fields (spec_ready, spec_pending), no blocked/pending — evidence: <grep + test>
- [x] Edit 3: parameter renamed task_store→work_store, type hint updated, callers updated — evidence: <grep build_awareness_snapshot callers>
- [x] Edit 4: existing tests updated for new schema — evidence: <pytest output, test count>
- [x] Edit 5: cancelled units counted only in total — evidence: <test name>
- [x] Edit 6: draft + spec-pending both count in spec_pending — evidence: <test name>
- [x] Edit 7: in-progress + auditing both count in in_progress — evidence: <test name>
- [x] Edit 8: no Phase 1-5 files modified — evidence: git status
```

Report: diffs, grep outputs, pytest output, COMPLETENESS block. Flag related issues, don't silently fix. Write when done.
