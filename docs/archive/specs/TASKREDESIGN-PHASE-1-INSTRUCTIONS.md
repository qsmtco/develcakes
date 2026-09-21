# Phase 1 — WorkUnit Model + Store + Singleton + Task Deprecation

**Loop authority:** `prompts/implementationLoop.md` + `prompts/implementationSupervisor.md`
**Spec:** `docs/specs/SPEC-TASK-SYSTEM-FULL-REDESIGN.md` (READ IN FULL — §2 is authoritative for this phase)
**Builder playbook:** `prompts/steelFramedCodeWriter.md` — load it fresh, start with the Discovery block, follow every rule.

## Files to change (3)

1. **`models/work_unit.py`** — NEW. Pure data layer. No imports from `ui/`, `gateway/`, `agent/`, or `models.task` (no circular import). Stdlib only (`dataclasses`, `datetime`, `typing`).
2. **`models/__init__.py`** — MODIFY. Add `work_store = WorkUnitStore()` singleton and re-export `WorkUnit`, `WorkUnitStore`, `work_store`. Keep ALL existing imports (Task, TaskStore, task_store, labels, constants) unchanged for backward compatibility. Update `__all__`.
3. **`models/task.py`** — MODIFY MODULE DOCSTRING ONLY. Add a deprecation notice at the top of the module docstring: `# DEPRECATED: superseded by models/work_unit.py (WorkUnit) as of SPEC-TASK-SYSTEM-FULL-REDESIGN. Kept for import compatibility only.` Do not change any code, fields, or logic in this file.

## What `models/work_unit.py` must contain (spec §2)

### `WorkUnit` dataclass (spec §2.1)

```python
@dataclass
class WorkUnit:
    id: str = field(default_factory=_work_next_id)
    title: str = ""
    spec_path: str = ""
    status: str = "draft"
    assigned_supervisor: str = "special:supervisor"
    assigned_builder: str = "special:coder"
    assigned_auditor: str = "special:debugger"
    priority: str = "medium"
    depends_on: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    completed_at: str = ""
    post_mortem_path: str = ""
    blocked_reason: str = ""
```

### Module-level counter + id factory (spec §2.1)

Mirrors the existing `models/task.py` pattern (`_task_next_num`, `_task_next_id`). Use names `_work_next_num` and `_work_next_id()` returning 8-char zero-padded strings.

### `_work_init_counter(work_units)` (spec §2.1)

Sets `_work_next_num` to `max(int(w.id)) + 1`, ignoring parse errors per unit. After loading units from disk, the persistence layer calls this. Spec gives exact code — follow it.

### Allowed constants

```python
WORK_STATUSES = ("draft", "spec-pending", "spec-ready", "in-progress", "auditing", "done", "cancelled")
WORK_PRIORITIES = ("low", "medium", "high", "critical")
WORK_STATUS_LABELS = { ... emoji labels ... }  # mirror models/task.py style
WORK_PRIORITY_LABELS = { ... }
```

### `WorkUnit.to_dict()` (spec §2.1 — use exact code given)

Must defensively copy `depends_on`: `"depends_on": list(self.depends_on)`.

### `WorkUnit.from_dict(cls, data)` (spec §2.1 — use exact code given)

Must validate types before accepting. Uses the nested `string_field` helper. Validates `depends_on` is a list of strings. Raises `ValueError` with descriptive messages. **Use the exact code in spec §2.1.**

### `WorkUnitStore` (spec §2.2)

```python
class WorkUnitStore:
    def create(self, work: WorkUnit) -> WorkUnit: ...
    def get(self, work_id: str) -> WorkUnit | None: ...
    def update(self, work: WorkUnit) -> WorkUnit: ...
    def list_all(self) -> list[WorkUnit]: ...
    def list_by_status(self, status: str) -> list[WorkUnit]: ...
    def delete(self, work_id: str) -> bool: ...
    def replace_all(self, work_units: Iterable[WorkUnit]) -> None: ...
```

Behavior (spec §2.2):
- Pure in-memory. **No file I/O.** Do not import `utils.work_persistence`.
- `create(work)`: assign sequential ID if `work.id` is empty; auto-stamp `created_at` and `updated_at` with ISO timestamp if empty (use `datetime.now().isoformat()`); store in internal dict; call `_work_init_counter([work])`-equivalent — actually: after assigning the id, advance the counter (the `_work_next_id` default_factory already advances; for explicit create with empty id, assign via the store). Return the work unit.
- `get(work_id)`: dict lookup.
- `update(work)`: stamp `updated_at = datetime.now().isoformat()`; store; return work.
- `list_all()`: sort by `created_at` ascending, ID as tiebreaker (empty `created_at` sorts first because `"" < "2026..."` lexicographically).
- `list_by_status(status)`: filter `list_all()`.
- `delete(work_id)`: `bool(self._work.pop(work_id, None))`.
- `replace_all(work_units)`: replace the internal dict contents. Used by the load path. Does NOT reset the counter; the persistence layer calls `_work_init_counter` separately.

## Dependencies / acyclicity (spec §2.1)

`depends_on` must be acyclic and must not include the unit's own ID. **Where to validate:** add a module-level helper `_validate_dependencies(work, existing_ids: set[str])` that raises `ValueError` on self-reference or unknown dependency ID. **Phase 1 scope:** define the helper and have `create()`/`update()` call it. Cycle detection across multiple units is checked in Phase 2 (persistence) or Phase 3 (handler); for Phase 1, implement self-reference rejection in `_validate_dependencies` and call it from `create` and `update` (passing the set of currently-known ids). Keep it simple and tested.

## Tests to create: `tests/test_work_unit.py` (spec §10)

Cover at minimum (≥30% sad-path):
- ID generation: two units get distinct 8-char zero-padded IDs; counter advances.
- Defaults: every field has the spec default.
- `to_dict` round-trip with `from_dict` preserves all fields including a populated `depends_on`.
- `from_dict` validates types: non-dict raises; non-string field raises; `depends_on` not a list raises; `depends_on` containing non-strings raises.
- `from_dict` defensive copy: mutating the input dict after `from_dict` does not mutate the unit's `depends_on`.
- `to_dict` defensive copy: mutating the returned `depends_on` does not mutate the unit.
- Status/priority validation: `_validate_dependencies` rejects self-reference.
- Store ordering: `list_all()` sorts by `created_at` then ID; empty `created_at` sorts first.
- Store `update` stamps `updated_at`.
- Store `delete` returns bool and removes only the matched id.
- Store `replace_all` swaps contents.
- `_work_init_counter` advances past loaded ids and ignores unparseable ids.

Run `python3 -m pytest tests/test_work_unit.py -v` and paste full output.

## Rules

- Use `prompts/steelFramedCodeWriter.md`. Start with the Discovery block listing files read.
- Run `python3 -c "from models.work_unit import WorkUnit, WorkUnitStore, _work_init_counter"` to prove import.
- Run `python3 -c "from models import work_store, WorkUnit, WorkUnitStore"` to prove singleton re-export.
- Run `python3 -m pytest tests/test_work_unit.py tests/test_tasks.py -v` — existing `tests/test_tasks.py` must still pass (task.py unchanged behaviorally).
- Do NOT modify `models/task.py` behavior — only the module docstring deprecation line.
- Do NOT import `utils.*` or `ui.*` from `models/work_unit.py`.
- For removals/changes to `models/__init__.py`: keep all existing exports intact.

## COMPLETENESS checklist (mandatory)

When you report back, include the literal block:

```
COMPLETENESS:
- [x] Edit 1: models/work_unit.py created — evidence: <wc -l>, <import output>
- [x] Edit 2: models/__init__.py exports work_store/WorkUnit/WorkUnitStore — evidence: <grep output>
- [x] Edit 3: models/task.py deprecation docstring — evidence: <head -5 output>
- [x] Edit 4: tests/test_work_unit.py created — evidence: <test count>, <pytest output>
- [x] Edit 5: existing tests/test_tasks.py still passes — evidence: <pytest output>
```

Report: files changed with line counts, full pytest output for both test files, the import-check outputs, and any related issues found (flagged, not silently fixed). Write when done.
