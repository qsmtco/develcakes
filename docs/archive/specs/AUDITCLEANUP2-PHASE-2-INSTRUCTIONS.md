# SPEC-AUDIT-CLEANUP-2 Phase 2 — Task-System Retirement (migrate `/status`, then delete)

**Spec:** `docs/specs/SPEC-AUDIT-CLEANUP-2-DEAD-CODE-SWEEP.md` — READ IN FULL. §"Phase 2" is authoritative.
**Builder playbook:** `prompts/steelFramedCodeWriter.md` — load fresh, Discovery block first, every rule.
**Supervisor:** special:supervisor | **Builder:** special:coder | **Auditor:** special:debugger
**Baseline:** tree clean at `0b4258c` (pushed? — supervisor pushes at unit close; local tip). Standing rules from AC1 in force (no resets, xvfb-run -a, pyflakes gate 0, re-grep before every deletion).

## THE ATOMICITY RULE (read first)

This phase has exactly **ONE commit**. The `/status` migration and the task-system deletion land together. Rationale: task-redesign post-mortem BUG#12 — if `cmd_status` migrates to `work_store` in one commit and `models/task.py` dies in the next, the intermediate tree has either a broken `/status` or a broken import. Neither state may exist.

## Verified consumer map (supervisor, 2026-09-07 evening — re-verify before editing)

| Surface | Location | Action |
|---|---|---|
| `/status` reads `task_store` | `ui/handlers/project_handler.py:19` (import), `:631-652` (`cmd_status`) | MIGRATE to `work_store` |
| Import-only, zero body usage | `ui/window.py:53` (`from models.task import Task`), `:54` (`task_store` in a combined import) | DELETE the imports |
| Dead handler (zero instantiations) | `ui/handlers/task_handler.py` (358 LOC) | DELETE the file |
| Data model + store | `models/task.py` (108 LOC) | DELETE the file |
| Singleton + exports | `models/__init__.py:20,29,62-66` | DELETE import line, `task_store = TaskStore()`, and the 5 task entries from `__all__` |
| Task-system tests | `tests/test_tasks.py` | DELETE the file |
| Awareness builder | `utils/project_awareness.py` | NO ACTION — already uses `work_store` (:758, :826); the word "task" elsewhere in that file refers to work units (verify :528-:621 are prose, not the deleted API — they are) |

## Step 2a — Migrate `cmd_status` (edit BEFORE deleting anything)

In `ui/handlers/project_handler.py`:
1. Change `:19` import to `from models import work_store`.
2. Rewrite the `cmd_status` body (:631-652) per the spec's mapping:
   - `all_tasks = task_store.list_all()` → `all_units = work_store.list_all()`
   - `t.assigned_to in members` → `u.assigned_builder in members`
   - Status buckets: `pending` = status in {draft, spec-pending, spec-ready}; `active` = {in-progress, auditing}; `blocked` = `blocked_reason` non-empty (any status); `done` = status == "done"; **cancelled units excluded from all counts**.
   - Output line: `f"Tasks: ..."` → `f"Work units: {pending} pending, {active} active, {blocked} blocked, {done} done"` — keep the other 4 lines (Project/Members/Review/Solo DM) byte-identical.
3. **Update the existing `cmd_status` test(s)** — grep `tests/` for `cmd_status` first; extend to cover the new bucket mapping (a unit per bucket: draft→pending, spec-ready→pending, in-progress→active, auditing→active, blocked_reason→blocked, done→done, cancelled→excluded). If no direct test exists, add one to the suite that tests `project_handler` (find it — likely `tests/test_project_handler*.py` or in a command-handler suite).

## Step 2b — Delete the task system (after 2a is green, same commit)

1. `rm ui/handlers/task_handler.py models/task.py tests/test_tasks.py`
2. `ui/window.py`: delete `:53` entirely; change `:54` to `from models import work_store`.
3. `models/__init__.py`: delete the `.task` import (`:20`), the singleton (`:29`), and the 5 `__all__` entries (`Task`, `TaskStore`, `task_store`, `TASK_STATUS_LABELS`, `PRIORITY_LABELS`) including the `# task` comment.
4. **Final repo grep (the gate):** `grep -rn "task_store\|models\.task\|TaskHandler\|TASK_STATUS_LABELS" --include='*.py' agent/ ui/ models/ utils/ gateway/ scripts/ main.py tests/ | grep -v work_` → **must be empty** (allow-listed: `work_persistence.py`'s legacy-migration code may reference `models.task` in comments/docstrings only — verify any hit there is a comment, not an import; if it's an import, STOP and report — the migration path may still need it).

## Verification (all pasted)

1. The repo grep above → empty.
2. `PYTHONDONTWRITEBYTECODE=1 python3 -c "import agent.runtime; import ui.window; from models import work_store"` → all OK.
3. `xvfb-run -a` suites: the `cmd_status` test suite (extended/new), `tests/test_work_handler.py` (the work system's own tests — must stay green), `tests/test_work_unit.py` + `tests/test_work_persistence.py` if they exist (grep first), `tests/test_command_handler.py`, `tests/test_project_awareness.py` (its `task_store=None` kwarg at :392 is a local-scope param name — verify it still passes; if it breaks because it imports the deleted API, fix THAT TEST's import, not the product).
4. pyflakes undefined-name → 0.
5. LOC accounting: `git show <commit> --stat` pasted (expect ~ -466 prod + -~150 test + small +/- in project_handler/window/__init__).

## Commit (1, atomic)

`refactor(task): retire task system — /status migrates to work_store, delete models/task.py + task_handler.py + test_tasks.py`

## COMPLETENESS checklist
- [ ] Discovery block (all touched files read in full)
- [ ] 2a migration done and its tests extended BEFORE any deletion (order in the diff is fine; logical order matters)
- [ ] Repo grep empty (paste it)
- [ ] All verification outputs pasted
- [ ] `models/__init__.py` `__all__` exactly matches its imports (no orphaned export names — python3 -c "from models import *" smoke)
- [ ] Related issues flagged, not silently fixed

**STOP after this phase.** Phase 3 (dead functions) is a separate delegation.
