# Phase 2 — Work Persistence + Legacy Migration

**Loop authority:** `prompts/implementationLoop.md` + `prompts/implementationSupervisor.md`
**Spec:** `docs/specs/SPEC-TASK-SYSTEM-FULL-REDESIGN.md` (READ §3 — Persistence and Migration — it is authoritative for this phase)
**Builder playbook:** `prompts/steelFramedCodeWriter.md` — load it fresh, start with the Discovery block, follow every rule.

## Files to change (1)

1. **`utils/work_persistence.py`** — NEW. Pure-Python persistence layer. May import from `models.work_unit` (WorkUnit, `_work_init_counter`) and `utils.project_awareness` (`get_crabcakes_dir`). **No imports from `ui/`, `gateway/`, or `agent/`.**

## Tests to create: `tests/test_work_persistence.py` (spec §10)

## Public API (spec §3.1 — exact)

```python
def work_json_path(project_path: str) -> str: ...        # <project>/.crabcakes/work.json
def tasks_summary_path(project_path: str) -> str: ...    # <project>/.crabcakes/tasks.md
def load_work_units(project_path: str) -> list[WorkUnit]: ...
def save_work_units(project_path: str, work_units: Iterable[WorkUnit]) -> None: ...
def load_or_migrate_work_units(project_path: str) -> list[WorkUnit]: ...
def render_tasks_summary(work_units: Iterable[WorkUnit]) -> str: ...
def write_tasks_summary(project_path: str, work_units: Iterable[WorkUnit]) -> None: ...
```

Use `get_crabcakes_dir(project_path)` — do NOT rebuild path conventions.

## JSON format (spec §3.1)

```json
{
  "version": 1,
  "work_units": [ { ...full WorkUnit.to_dict()... } ]
}
```

## Behavior requirements

### `work_json_path` / `tasks_summary_path`
Return `<project_path>/.crabcakes/work.json` and `<project_path>/.crabcakes/tasks.md` respectively (use `os.path.join` with `get_crabcakes_dir`).

### `load_work_units(project_path)`
- Missing file → `[]`.
- Invalid JSON / wrong top-level shape (not dict, missing `version`, missing/malformed `work_units` list) → log a warning, return `[]`. **Never crash.**
- Each record parsed via `WorkUnit.from_dict()`. A malformed record raises `ValueError`; catch per-record, log, skip (do NOT abort the whole load on one bad record — best-effort).
- **After loading**, call `_work_init_counter(loaded_units)` to advance the model counter past loaded IDs (avoids restart collisions).
- Do NOT call `_work_init_counter` on the empty-list path (counter stays where the model left it — the load path's contract is "advance past loaded ids"; no ids, nothing to advance past).

### `save_work_units(project_path, work_units)`
- Create `.crabcakes/` if needed (via `_ensure_crabcakes_dir` or `os.makedirs(..., exist_ok=True)`).
- Write `work.json` as `{"version": 1, "work_units": [w.to_dict() for w in work_units]}`.
- **Atomicity:** write to a temp file in the same directory then `os.replace()` onto the final path (matches the repo's file-writing conventions for crash safety). Spec §3.1: "writes valid JSON atomically where the repository's file-writing conventions permit."
- Then call `write_tasks_summary(project_path, work_units)`.
- **A failed summary write must NOT corrupt the JSON source of truth.** Wrap the summary write in try/except, log the error, preserve `work.json`. (i.e., the JSON write + os.replace must complete and be durable BEFORE the summary write, and a summary failure must not delete/rollback the JSON.)

### `load_or_migrate_work_units(project_path)` (spec §3.2 — exact)
1. If `work.json` exists and parses into the versioned shape → load it directly via `load_work_units`, regenerate `tasks.md` from it (call `write_tasks_summary`), return the loaded list.
2. If `work.json` is absent → parse existing `tasks.md` best-effort (one Work Unit per recognizable task section/table row, spec §3.2 example), map legacy statuses, persist to `work.json` once, regenerate `tasks.md` once. Return migrated list.
3. If no recognizable tasks in `tasks.md` → return `[]` (do not write anything).
4. **Migration is one-shot:** persist migrated units to `work.json` exactly once. Re-opening a project must not duplicate migrated units (this falls out naturally because step 1 sees the now-existing `work.json`).
5. Never delete or overwrite the original `tasks.md` content before the new JSON has been successfully written.

**Legacy status mapping (spec §3.2):**
- `pending` → `draft` (NOT spec-ready — no spec exists)
- `in_progress` → `in-progress`
- `blocked` → `in-progress` + `blocked_reason` set from notes
- `done` → `done`
- `cancelled` → `cancelled`

**Legacy markdown parsing (spec §3.2 example input):**
```markdown
## Task 00000003: File watcher core — 🔄 in_progress
- **Priority:** high
- **Assigned:** special:coder

## Task 00000004: API integration — 🚫 blocked
- **Priority:** medium
- **Notes:** waiting for credentials
```
Yields: unit `00000003` (`in-progress`, priority `high`, spec_path empty), unit `00000004` (`in-progress`, priority `medium`, blocked_reason `"waiting for credentials"`, spec_path empty).

The parser should handle the heading `## Task <ID>: <title> — <status-emoji-and-text>` and bullet lines `- **Priority:** X`, `- **Assigned:** Y`, `- **Notes:** Z`. Be defensive — a section that doesn't match the heading regex is skipped (don't crash on arbitrary markdown). Unparseable markdown is retained as legacy text only; never fabricated into completed work.

### `render_tasks_summary(work_units)` (spec §3.1)
- Deterministic, stable human-readable format.
- Include every Work Unit: ID, title, status, priority, spec indicator/path, assignments.
- Header MUST contain the source-of-truth note: `Generated from `.crabcakes/work.json`; edit work units through `/work` commands.`
- Sort by the same order as `WorkUnitStore.list_all()` (created_at asc, then id) for determinism — accept an `Iterable` but sort a materialized list.
- **No implementation path may parse this generated summary after writing it.**

### `write_tasks_summary(project_path, work_units)`
- Render via `render_tasks_summary` and write to `tasks_summary_path(project_path)`. Create `.crabcakes/` if needed. Log on OSError, do not raise (best-effort).

## Tests (`tests/test_work_persistence.py`) — spec §10, ≥30% sad path

Use `tempfile.TemporaryDirectory()` for a project root. Cover:
- `work_json_path` / `tasks_summary_path` return correct paths.
- JSON round-trip: save a few units, load them back, all fields preserved including `depends_on` and empty-string fields. Counter advances past loaded IDs (create a new WorkUnit after load, confirm its id > max loaded id).
- Missing file → `[]`, no crash, no file created.
- Invalid JSON → warning logged, `[]`, no crash.
- Wrong top-level shape (e.g. `{"version": 1}` missing `work_units`, or `work_units` not a list) → `[]`.
- Malformed record in the list (one good + one `{"id": "x"}` with bad type) → good record loads, bad one skipped, no crash.
- Atomic save: write a sentinel to `work.json`, then have `write_tasks_summary` fail (monkeypatch `render_tasks_summary` to raise, or write to a read-only dir for the summary path) — confirm `work.json` is intact and unchanged.
- `render_tasks_summary` is deterministic: two calls with the same units produce identical strings; header contains the source-of-truth note; spec indicator distinguishes missing (`⚠`) vs present (`✓`) spec_path.
- **Generated-summary non-readback:** after `save_work_units` writes `tasks.md`, confirm `load_work_units` reads ONLY from `work.json` (mutate `tasks.md`, reload, confirm no change).
- **Legacy migration:** seed a `tasks.md` with the spec §3.2 example; call `load_or_migrate_work_units` on a project with no `work.json`; confirm 2 migrated units with correct statuses/blocked_reason; confirm `work.json` was written; confirm `tasks.md` was regenerated.
- **Migration idempotence:** call `load_or_migrate_work_units` twice on a migrated project; confirm no duplicate units (count stays at 2).
- **Migration no-op when nothing recognizable:** seed a `tasks.md` with random prose (no `## Task` headings); confirm `[]` returned and no files written.
- **Sad path:** `tasks.md` with a heading but unparseable body → skip gracefully, no crash.

Run `python3 -m pytest tests/test_work_persistence.py tests/test_work_unit.py -v` and paste full output.

## Rules

- `prompts/steelFramedCodeWriter.md` — start with Discovery block.
- Verify import: `python3 -c "from utils.work_persistence import work_json_path, tasks_summary_path, load_work_units, save_work_units, load_or_migrate_work_units, render_tasks_summary, write_tasks_summary"`
- Verify no forbidden imports: `grep -nE "^(import|from)\s+(ui|gateway|agent)" utils/work_persistence.py` → must be empty.
- Do NOT modify `models/work_unit.py` or any other Phase 1 file.
- The `_work_init_counter` call site is here (per the Phase 1 audit's flagged critical-path note). Confirm via grep that it is called inside `load_work_units` after the units are parsed.

## COMPLETENESS checklist (mandatory)

```
COMPLETENESS:
- [x] Edit 1: utils/work_persistence.py created — evidence: <wc -l>, <import output>, <forbidden-import grep = empty>
- [x] Edit 2: tests/test_work_persistence.py created — evidence: <test count>, <pytest output>
- [x] Edit 3: _work_init_counter called in load_work_units — evidence: <grep -n _work_init_counter utils/work_persistence.py>
- [x] Edit 4: atomic save (os.replace) implemented — evidence: <grep -n os.replace>
- [x] Edit 5: legacy migration one-shot + idempotent — evidence: <test names + output>
- [x] Edit 6: failed summary write does not corrupt JSON — evidence: <test name + output>
```

Report: files changed with line counts, full pytest output, the grep outputs, and any related issues (flagged, not silently fixed). Write when done.
