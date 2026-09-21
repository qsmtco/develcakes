# Phase 3 — Work Handler (Commands + Supervisor Handoff)

**Loop authority:** `prompts/implementationLoop.md` + `prompts/implementationSupervisor.md`
**Spec:** `docs/specs/SPEC-TASK-SYSTEM-FULL-REDESIGN.md` (READ §4 — Work Commands and Handler — it is authoritative for this phase)
**Builder playbook:** `prompts/steelFramedCodeWriter.md` — load it fresh, start with the Discovery block, follow every rule.

## Files to change (1)

1. **`ui/handlers/work_handler.py`** — NEW. GTK-free command handler. May import: `models.work_unit` (WorkUnit, WorkUnitStore, WORK_STATUSES, WORK_PRIORITIES, WORK_STATUS_LABELS, WORK_PRIORITY_LABELS), `models.command` (Command, CommandResult), `models.feed_card` (FeedCardData), `utils.work_persistence` (load_or_migrate_work_units, save_work_units, write_tasks_summary), `utils.project_awareness` (load_team). **No imports from `agent/` or `gateway/`**; may receive `agent_runtime_handler` via constructor injection but must NOT import its module.

## Tests to create: `tests/test_work_handler.py` (spec §10)

## Verified APIs to use (read these files yourself to confirm before coding)

- `models/work_unit.py` (Phase 1): `WorkUnit`, `WorkUnitStore` (`create`/`get`/`update`/`list_all`/`list_by_status`/`delete`/`replace_all`), `WORK_STATUSES`, `WORK_PRIORITIES`, `_validate_dependencies`.
- `utils/work_persistence.py` (Phase 2): `load_or_migrate_work_units(project_path) -> list[WorkUnit]`, `save_work_units(project_path, work_units) -> None`, `write_tasks_summary(project_path, work_units) -> None`.
- `utils/project_awareness.py`: `load_team(project_path) -> ProjectTeam`. `ProjectTeam` (models/team.py) has `.members` (list[TeamMember]), `.pm_id` (str), `.pm_name` (str), `.has_member(session_key) -> bool`, `.get_member(session_key) -> TeamMember | None`.
- `models/command.py`: `Command(name, args, flags, raw_text, body, source_session_key, target_session_key, is_broadcast, broadcast_targets, user)`. `CommandResult(handled, response_text, response_card, forward_to, forward_text)`.
- `project_handler` (injected): `get_active_project_name() -> str | None`, `get_active_project_path() -> str | None`, `get_project_members(project_name) -> list[str]`.
- `agent_runtime_handler` (injected, optional): `send_to_special_agent(session_key: str, text: str) -> None` — returns None, starts runtime async.

## Constructor (spec §4.1 — exact signature)

```python
class WorkHandler:
    def __init__(
        self,
        project_handler,
        work_store,
        agent_runtime_handler=None,
        on_display_card=None,
        on_display_text=None,
        on_feed_card=None,
        GLib_module=None,
    ): ...
```

The handler must resolve the active project path/name through `project_handler`'s public APIs and must NOT read `ui.window` state directly. It may call the injected `agent_runtime_handler.send_to_special_agent(session_key, text)` for `/work start`; it must NOT import `agent_runtime_handler` or `agent.runtime` modules.

## State owned by the handler

- `self._project_path: str | None` — set by `load_for_project(path)` (spec §3.3). Cleared by `close_project()`.
- The handler does NOT own the work store lifecycle; it uses the injected `work_store`. `load_for_project(path)` calls `load_or_migrate_work_units(path)` then `work_store.replace_all(loaded)`.

## Lifecycle methods

```python
def load_for_project(self, project_path: str) -> None:
    """Load (or migrate) work units for a project and bind the store. Called by window.py on project open."""
    self._project_path = project_path
    loaded = load_or_migrate_work_units(project_path)
    self._work_store.replace_all(loaded)

def close_project(self) -> None:
    """Release the active project binding (does NOT delete persisted data)."""
    self._project_path = None
```

## Command methods (spec §4.2, §4.3, §4.4) — all return CommandResult

Project-scoped commands must return a clear response when no project is active (e.g. `"Open a project first."`). Work-unit IDs accept the 8-digit form and `#`-prefixed form; no title matching. The parser must NOT mutate the shared `Command.args` unexpectedly — use a local view.

### `cmd_work(self, cmd: Command) -> CommandResult`
The canonical `/work` command. Behavior depends on `cmd.args`:
- No args (or empty) → **create a draft** Work Unit. Title derivation: if `cmd.body` is non-empty (quoted input like `/work "My title"`), use `cmd.body`; else join `cmd.args` with spaces (unquoted like `/work My title`). Create `WorkUnit(title=..., status="draft", spec_path="")`, call `work_store.create`, persist (`save_work_units`), regenerate summary (`write_tasks_summary`), return a response card. **Must NOT claim spec-ready.**
- `cmd.args[0] == "list"` → delegate to `cmd_work_list`.
- `cmd.args[0] == "start"` → delegate to `cmd_work_start`.
- `cmd.args[0] in {"done","blocked","cancel","assign","priority","spec-ready","status","unblock"}` → delegate to the corresponding `cmd_work_*` method.
- Unknown subcommand → usage error.

Also detect legacy-vs-canonical form from `cmd.name` (`"work"` vs `"task"`): `/task` maps ONLY to draft creation (ignore any subcommand-looking args; treat the rest as title). `/work` with no subcommand creates a draft.

### `cmd_work_list` (alias `/tasks`)
Output: ID, title, status, priority, spec indicator (`⚠` missing vs `✓` present — based on whether `spec_path` is non-empty AND the file exists under project root), supervisor, builder, auditor. Empty list → `"No work units yet."`.

### `cmd_work_start` (alias `/start`) — THE KEY NEW BEHAVIOR (spec §4.4)
Execute synchronously through validation, then dispatch async via the runtime handler:
1. Resolve project path; load Work Unit N (parse ID from `cmd.args[1]`, strip `#`).
2. Missing ID/unit → CommandResult error.
3. **Spec validation**: `spec_path` non-empty AND relative. Resolve full path with `os.path.realpath(os.path.join(project_path, spec_path))`; compare `os.path.normcase(resolved_path)` against `os.path.normcase(os.path.realpath(project_path))` + path separator; reject absolute paths, `..` traversal, and symlinks escaping root. Then `os.path.isfile(resolved_path)` must be True. Missing spec → exactly: `"Work unit #N has no spec. Write the spec first."`
4. **Dependency check** (spec §4.4 step 3.5): for each id in `work_unit.depends_on`, look it up in the store. Build ONE error list distinguishing missing deps from unfinished ones: `"#B (not found)"` when absent, `"#A (status: in-progress)"` when present but not done. If any unresolved → exactly: `"Work unit #N has unresolved dependencies: #A (status: in-progress), #B (not found). Resolve dependencies first."` Do not change state or send.
5. **Status check**: `status == "spec-ready"` AND `blocked_reason == ""`; else return a message explaining the required status/recovery action.
6. **Supervisor membership**: `assigned_supervisor` must be in `project_handler.get_project_members(project_name)`. If absent → exactly: `"Add the Supervisor agent to begin implementation."` Do not send or set in-progress.
7. Set `status="in-progress"`, stamp `updated_at`, persist (save_work_units + write_tasks_summary) BEFORE sending.
8. Construct message (exact): `f"Load prompts/implementationLoop.md. This work unit's spec is at {spec_path}. Begin the implementation loop."`
9. Call `agent_runtime_handler.send_to_special_agent(assigned_supervisor, message)` **exactly once**. Wrap in `try/except`. On a synchronous exception, log it, **roll status back to `spec-ready`**, persist, return a failure CommandResult; the exception must NOT escape `process_input()`. On success (`None`), status stays `in-progress`.
10. Return a response confirming the Work Unit ID and Supervisor handoff.

### `cmd_work_done` (alias `/done`) — spec §4.3
PM or assigned Supervisor only. Validate target, set `status="done"`, set `completed_at`, persist, regenerate summary, report. Refuse missing unit; must not silently change a cancelled unit back to done. Authorization: see §4.6.

### `cmd_work_blocked` (alias `/blocked`) — spec §4.3
Requires non-empty reason (from `cmd.body` or `cmd.args[2:]`). Sets `status="in-progress"` + `blocked_reason=reason` (do NOT add a `blocked` status — the approved status set has no `blocked`). Persist + report.

### `cmd_work_unblock` — spec §4.3
Valid only when `status == "in-progress"` AND `blocked_reason` is non-empty. Before restoring readiness, re-validate `spec_path`: non-empty AND file exists safely under project root. If spec missing → clear blocked reason, transition to `draft`, persist, report exactly: `"Spec file no longer exists. Work unit reverted to draft."` If spec exists → clear `blocked_reason`, restore `status="spec-ready"`, persist, report. Other states → clear refusal.

### `cmd_work_cancel` (alias `/cancel`) — spec §4.3, §4.6
PM only. Set `status="cancelled"`, persist, report.

### `cmd_work_assign` (alias `/assign`) — spec §4.5
Identify whether target is supervisor/builder/auditor. Preserve `Command` mention resolution (`cmd.target_session_key`); do NOT infer role only from display-name substrings when a session key is available. Update exactly ONE assignment field; return a usage/error if role is ambiguous.

### `cmd_work_priority` (alias `/priority`)
Parse level from `cmd.args[2]`; validate against `WORK_PRIORITIES`; set, persist, report.

### `cmd_work_spec_ready` — spec §4.3
Validate non-empty relative `spec_path`; resolve safely under project root (same realpath+normcase containment as `/work start`); verify file exists; transition only from `draft` or `spec-pending` to `spec-ready`. Reject absolute paths and `..` traversal. If assigned Supervisor not in project team → warning (NOT hard refusal) after marking ready: `"Spec marked ready, but Supervisor is not in the project team. Add Supervisor before /work start."` Persist + report.

### `cmd_work_status` — spec §4.3 transition table
Apply the EXPLICIT transition + authorization table from spec §4.3:
- `draft`: from any non-done; PM or assigned Supervisor.
- `spec-pending`: from `draft`; PM or assigned Supervisor.
- `spec-ready`: **REJECTED** — use `/work spec-ready #N`.
- `in-progress`: **REJECTED** — use `/work start #N`.
- `auditing`: from `in-progress`; assigned Supervisor only.
- `done`: **REJECTED** — use `/work done #N`.
- `cancelled`: from any non-done; PM only.
Enforce the table; persist every accepted transition.

## Authorization (spec §4.6)
PM identity = `cmd.source_session_key` matches `project:<name>` (user in project tab) OR the project team's `pm_id` (use `ProjectTeam.pm_id` — do NOT invent a second identity store). `/work done` and `/work cancel` authorized for PM identity OR assigned Supervisor. `/work status`, `/work spec-ready`, `/work unblock`, lifecycle mutations → PM/Supervisor authorization; unrelated gateway agents denied. Tests must cover PM, assigned Supervisor, unrelated agent, missing project, missing member cases.

## Path-containment helper (used by start + spec-ready + unblock)
Write a private `_spec_path_within_project(project_path, spec_path) -> tuple[bool, str]` returning (ok, resolved_path_or_errmsg). Implement with `os.path.realpath` + `os.path.normcase` + separator comparison (spec §4.4 step 3). Reject absolute paths, `..` traversal, symlink-escape. Reuse it everywhere a spec path is validated — do NOT inline three copies.

## Persistence helper
Write a private `_persist(self) -> None` that calls `save_work_units(self._project_path, self._work_store.list_all())` (save_work_units already regenerates the summary). Call it after every mutation. Return CommandResult error if `_project_path is None`.

## Rules
- `prompts/steelFramedCodeWriter.md` — start with Discovery block listing files read.
- Verify import: `python3 -c "from ui.handlers.work_handler import WorkHandler"`.
- Verify no forbidden imports: `grep -nE "^(import|from)\s+(agent|gateway)" ui/handlers/work_handler.py` → must be empty. (`from models...`, `from utils...` are fine.)
- Do NOT modify Phase 1 or Phase 2 files.
- All command methods return CommandResult; failures become `response_text`, never leaked exceptions.
- ≥30% sad-path tests.

## Tests (`tests/test_work_handler.py`) — spec §10
Use a fake project_handler (with `get_active_project_name`/`get_active_project_path`/`get_project_members`), a real `WorkUnitStore`, a fake agent_runtime_handler recording `send_to_special_agent` calls, and a `tempfile.TemporaryDirectory()` for the project root. Cover:
- `cmd_work` create (quoted body + unquoted args), draft status, empty spec_path, persistence.
- `cmd_work` subcommand routing (list/start/done/etc).
- `/task` legacy create (title only, ignores subcommand args).
- `cmd_work_list` empty → "No work units yet."; populated → shows all fields + spec indicator.
- **`cmd_work_start` happy path**: spec file seeded, status spec-ready, supervisor in members, blocked_reason empty → status→in-progress, persisted, `send_to_special_agent` called EXACTLY ONCE with the exact implementation-loop message, returns confirmation.
- **`cmd_work_start` sad paths**: missing unit; missing spec (exact message); absolute/`..`/symlink-escape spec_path rejected; unresolved dependencies (not-found vs not-done in one error list, exact message); wrong status; blocked_reason non-empty; supervisor NOT in members (exact message "Add the Supervisor agent to begin implementation."); send raises → status rolled back to spec-ready, failure returned, exception does not escape.
- `cmd_work_done`: happy (status→done, completed_at set); refused for cancelled; authorization (PM ok, assigned Supervisor ok, unrelated agent denied).
- `cmd_work_blocked`: reason required; sets in-progress + blocked_reason (NOT a "blocked" status).
- `cmd_work_unblock`: in-progress+blocked → spec-ready (spec exists); spec missing → draft (exact message); other states → refusal.
- `cmd_work_cancel`: PM-only authorization; cancelled stays cancelled.
- `cmd_work_assign`: role disambiguation; ambiguous → error.
- `cmd_work_priority`: valid level; invalid level rejected.
- `cmd_work_spec_ready`: draft/spec-pending → spec-ready (spec exists); missing spec rejected; traversal rejected; supervisor-not-in-team warning (not refusal).
- `cmd_work_status`: each row of the transition table (accepted transitions + the 3 REJECTED statuses + authorization).
- `load_for_project` + `close_project` lifecycle (store replaced on load; path cleared on close).
- Project-scope errors: no active project → clear response, no state mutation.

Run `python3 -m pytest tests/test_work_handler.py tests/test_work_persistence.py tests/test_work_unit.py -v` and paste full output.

## COMPLETENESS checklist (mandatory)

```
COMPLETENESS:
- [x] Edit 1: ui/handlers/work_handler.py created — evidence: <wc -l>, <import output>, <forbidden-import grep empty>
- [x] Edit 2: tests/test_work_handler.py created — evidence: <test count>, <pytest output>
- [x] Edit 3: cmd_work_start calls send_to_special_agent EXACTLY ONCE with exact message — evidence: <test name + assertion>
- [x] Edit 4: path containment helper reused (not inlined 3x) — evidence: <grep -c "_spec_path_within_project">
- [x] Edit 5: status transition table enforced — evidence: <test names for each row>
- [x] Edit 6: authorization (PM/Supervisor/unrelated) tested — evidence: <test names>
- [x] Edit 7: load_for_project/close_project lifecycle — evidence: <test names>
- [x] Edit 8: no forbidden imports (agent/gateway) — evidence: <grep empty>
```

Report: files changed with line counts, full pytest output, grep outputs, and any related issues (flagged, not silently fixed). Write when done.
