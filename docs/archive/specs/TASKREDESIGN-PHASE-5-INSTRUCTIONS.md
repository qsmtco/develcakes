# Phase 5 — window.py: Constructor Rename + Project Lifecycle Wiring

**Loop authority:** `prompts/implementationLoop.md` + `prompts/implementationSupervisor.md`
**Spec:** `docs/specs/SPEC-TASK-SYSTEM-FULL-REDESIGN.md` (§5.2 + §3.3 + §11 — authoritative)
**Builder playbook:** `prompts/steelFramedCodeWriter.md` — load fresh, Discovery block, follow every rule.

## Context

Phase 4's BUG #20 fix (done by Supervisor) already swapped `TaskHandler`→`WorkHandler` construction in window.py and wired the deps. What remains in Phase 5:
1. Rename the constructor param `task_handler` → `work_handler` in `command_handler.py` AND the window.py call site (cosmetic clarity — Phase 4 deferred this).
2. Wire the **project lifecycle** so Work Units load/migrate on project open and release on close (spec §3.3, §11). This is the functional part.

## Files to change (2)

1. **`ui/handlers/command_handler.py`** — MODIFY. Rename constructor param `task_handler` → `work_handler` and instance attr `self._task_handler` → `self._work_handler`. Update the comment. The registration block already calls `cmd_work*` methods — just update the `if work_handler is not None:` guard and the `task_handler.cmd_work` → `work_handler.cmd_work` references.
2. **`ui/window.py`** — MODIFY. (a) Rename `self._task_handler` → `self._work_handler` everywhere it appears (the attr decl at ~line 85, the construction at ~line 600, the CommandHandler call at ~line 642). (b) Add project-lifecycle wiring (see below).

## Project lifecycle wiring (spec §3.3, §11) — the functional change

Spec §11 data flow: "On project open, `ui/window.py` invokes `work_handler.load_for_project(path)` from the project-open callback; that method calls `load_or_migrate_work_units(path)` and then `work_store.replace_all(loaded)`. This binding occurs before snapshots or commands."

Mirror the existing ReviewHandler pattern (window.py ~lines 787-792):
```python
self._project_handler.set_on_project_opened(
    lambda n, p: (self._review_handler.on_project_opened(n, p))
)
self._project_handler.set_on_project_closed(
    lambda name: (self._review_handler.on_project_closed(name))
)
```

Add the equivalent for the work handler, placed near the ReviewHandler wiring (or near the existing `set_on_project_opened`/`set_on_project_closed` calls):
```python
# Work Handler lifecycle: load/migrate Work Units on open, release on close
# (SPEC-TASK-SYSTEM-FULL-REDESIGN §3.3, §11). Must bind the store BEFORE any
# /work command or awareness snapshot can read it.
self._project_handler.set_on_project_opened(
    lambda n, p: self._work_handler.load_for_project(p)
)
self._project_handler.set_on_project_closed(
    lambda name: self._work_handler.close_project()
)
```

`load_for_project(path)` and `close_project()` already exist on `WorkHandler` (Phase 3). Do NOT call them inline in `_on_project_opened`/`_on_project_closed` — use the `set_on_project_opened`/`set_on_project_closed` callback registration so the wiring is consistent with the other handlers and ProjectHandler owns the dispatch.

**Important:** `set_on_project_opened` can be called multiple times — each call adds a callback to the handler's list (verify this by reading ProjectHandler if unsure; the ReviewHandler wiring already relies on this). Do NOT replace the existing callbacks.

## Verified APIs (read the files to confirm before editing)
- `WorkHandler.load_for_project(project_path: str) -> None` (Phase 3)
- `WorkHandler.close_project() -> None` (Phase 3)
- `ProjectHandler.set_on_project_opened(cb)` / `set_on_project_closed(cb)` — callback registration; cb signature is `(name, path)` for opened, `(name)` for closed.

## Tests (`tests/test_work_handler.py` or a new `tests/test_window_work_lifecycle.py`)

The lifecycle methods were tested in Phase 3 (`test_load_for_project_binds_store`, `test_close_project_clears_binding`). For Phase 5, add a focused test that verifies the window.py wiring invokes load on open and close on close. Since constructing the full MainWindow requires GTK, write a **unit test that verifies the callback registration** by using a fake project handler that records `set_on_project_opened`/`set_on_project_closed` calls — OR verify via grep/inspection that the wiring lines exist. If GTK prevents a true integration test, document that and add a static assertion (grep for `load_for_project` and `close_project` in window.py).

Run `python3 -m pytest tests/test_command_handler.py tests/test_work_handler.py tests/test_work_persistence.py tests/test_work_unit.py -v` and paste full output.

## Rules
- `prompts/steelFramedCodeWriter.md` — Discovery block first.
- Do NOT modify Phase 1/2/3 files (work_unit.py, work_persistence.py, work_handler.py).
- Verify after edit: `python3 -c "import ast; ast.parse(open('ui/window.py').read()); ast.parse(open('ui/handlers/command_handler.py').read()); print('syntax OK')"`.
- Verify the rename is complete: `grep -n "task_handler" ui/handlers/command_handler.py ui/window.py` — should return ZERO matches (except possibly in comments explaining the rename history, which you may keep or remove).
- Verify lifecycle wiring: `grep -n "load_for_project\|close_project" ui/window.py` — both must appear.
- Existing non-task command wiring unchanged.

## COMPLETENESS checklist (mandatory)

```
COMPLETENESS:
- [x] Edit 1: command_handler.py task_handler→work_handler rename (param + attr + guard + registration refs) — evidence: grep task_handler = 0 matches
- [x] Edit 2: window.py self._task_handler→self._work_handler rename (decl + construction + call site) — evidence: grep task_handler = 0 matches
- [x] Edit 3: window.py project-lifecycle wiring (set_on_project_opened→load_for_project, set_on_project_closed→close_project) — evidence: grep load_for_project + close_project in window.py
- [x] Edit 4: existing callbacks NOT replaced (set_on_project_opened adds, not replaces) — evidence: <read ProjectHandler.set_on_project_opened to confirm append semantics>
- [x] Edit 5: full requested suite green — evidence: pytest output
- [x] Edit 6: no Phase 1/2/3 files modified — evidence: git status
```

Report: diffs, grep outputs, pytest output, COMPLETENESS block. Flag related issues, don't silently fix. Write when done.
