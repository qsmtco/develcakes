# Phase 4 — Command Registration (no `aliases=` for work commands)

**Loop authority:** `prompts/implementationLoop.md` + `prompts/implementationSupervisor.md`
**Spec:** `docs/specs/SPEC-TASK-SYSTEM-FULL-REDESIGN.md` (READ §5.1 — it is authoritative for this phase)
**Builder playbook:** `prompts/steelFramedCodeWriter.md` — load it fresh, start with the Discovery block, follow every rule.

## Files to change (1)

1. **`ui/handlers/command_handler.py`** — MODIFY. Replace the existing task-handler registration block with Work Handler registrations.

## Tests to update: `tests/test_command_handler.py` (spec §10)

## Critical gotcha (spec §5.1) — read this twice

`CommandRegistry.get()` checks `_commands` BEFORE `_aliases` (verified at `models/command.py:144-147`). This means:

- Registering `/work` with `aliases=["task"]` would register `task` only as an **alias**, but `get("task")` would still work — HOWEVER, if `task` is ALSO ever registered as a canonical command, the canonical wins and the alias is orphaned. The spec forbids this collision.
- **Therefore: register `/work` and EVERY legacy name (`task`, `tasks`, `start`, `done`, `blocked`, `cancel`, `assign`, `priority`) as SEPARATE canonical commands.** Do NOT use the `aliases=` parameter for ANY work command. This is mandatory per spec §5.1.

Spec §5.1 gives the exact registration code — follow it. Each registration uses `payload_free=True` (this permits command parsing without requiring a quoted payload while still allowing `/work` creation to consume `cmd.body`).

## What to change

### Constructor parameter (spec §5.1)

Rename the constructor param `task_handler` → `work_handler` (and the instance attr `self._task_handler` → `self._work_handler`). Keep it optional (`=None`). Update the comment. The constructor stores it the same way.

### Registration block (spec §5.1 — exact code)

Find the existing block:
```python
        # Task — requires TaskHandler
        if task_handler is not None:
            self.register_command("task", task_handler.cmd_task, aliases=["t"], ...)
            self.register_command("done", task_handler.cmd_done, ..., payload_free=True)
            ... (8 registrations)
```

Replace it ENTIRELY with (spec §5.1):
```python
        # Work — requires WorkHandler (SPEC-TASK-SYSTEM-FULL-REDESIGN §5.1)
        # CRITICAL: register every legacy name as a SEPARATE canonical command.
        # Do NOT use aliases= for any work command — CommandRegistry.get() checks
        # _commands before _aliases, so registering /work with aliases=["task"]
        # would orphan the legacy /task command. payload_free=True for all.
        if work_handler is not None:
            self.register_command("work", work_handler.cmd_work,
                help_text="Work units: create, list, start, spec-ready, status, unblock, done, blocked, cancel, assign, priority",
                payload_free=True)
            self.register_command("task", work_handler.cmd_work,
                help_text="Create a Work Unit (legacy alias)", payload_free=True)
            self.register_command("tasks", work_handler.cmd_work_list,
                help_text="List Work Units (legacy alias)", payload_free=True)
            self.register_command("start", work_handler.cmd_work_start,
                help_text="Start a Work Unit (legacy alias)", payload_free=True)
            self.register_command("done", work_handler.cmd_work_done,
                help_text="Complete a Work Unit (legacy alias)", payload_free=True)
            self.register_command("blocked", work_handler.cmd_work_blocked,
                help_text="Block a Work Unit (legacy alias)", payload_free=True)
            self.register_command("cancel", work_handler.cmd_work_cancel,
                help_text="Cancel a Work Unit (legacy alias)", payload_free=True)
            self.register_command("assign", work_handler.cmd_work_assign,
                help_text="Assign a Work Unit (legacy alias)", payload_free=True)
            self.register_command("priority", work_handler.cmd_work_priority,
                help_text="Set Work Unit priority (legacy alias)", payload_free=True)
```

Notes:
- `/work` and `/task` BOTH route to `work_handler.cmd_work` (the handler detects legacy-vs-canonical from `cmd.name`).
- The other legacy names (`tasks`, `start`, `done`, `blocked`, `cancel`, `assign`, `priority`) each route to their corresponding `cmd_work_*` method.
- `/work` subcommands (`list`, `start`, `spec-ready`, `status`, `unblock`, `done`, `blocked`, `cancel`, `assign`, `priority`) are handled INSIDE `cmd_work` by argument routing (Phase 3 already implements this).
- **All registrations use `payload_free=True`** — this is what lets `/work My title` (unquoted) and `/work "My title"` (quoted) both work, while `/start #N` etc. don't require a quoted payload.
- `/work` must be canonical in help output.

## Other call sites to check (do not break)

- `window.py` constructs `CommandHandler(..., task_handler=self._task_handler, ...)`. **Phase 5 will rewrite window.py to pass `work_handler` instead.** For Phase 4, you must keep `window.py` working. Two options:
  - **Option A (preferred):** keep the param name `task_handler` in the constructor signature but treat it as the work handler internally. This avoids touching window.py in Phase 4.
  - **Option B:** rename to `work_handler` and also update window.py's call site in the SAME phase.
  
  **Use Option A** — keep the constructor param as `task_handler` for now (window.py still passes `self._task_handler`, which Phase 5 will rewire to a `WorkHandler` instance). The internal registration block uses the param directly. Phase 5 renames the param and the window.py call site together. This keeps Phase 4 a single-file change.

  **Revised instruction:** Do NOT rename the constructor param in Phase 4. Keep `task_handler=None` and `self._task_handler`. Only change the registration block to call work-handler methods (`cmd_work`, `cmd_work_list`, etc.) on the passed-in handler. Phase 5 will handle the rename.

## Tests (`tests/test_command_handler.py`) — spec §10

Read the existing test file first to mirror its style. Add/update tests:
- **Canonical `/work` registration**: `/work` resolves to the work handler's `cmd_work`.
- **Legacy `/task` routes to `cmd_work`**: register a fake work_handler, send `/task`, confirm `cmd_work` was the handler invoked (not a separate `cmd_task`).
- **Each legacy name routes to its method**: `/tasks`→`cmd_work_list`, `/start`→`cmd_work_start`, `/done`→`cmd_work_done`, `/blocked`→`cmd_work_blocked`, `/cancel`→`cmd_work_cancel`, `/assign`→`cmd_work_assign`, `/priority`→`cmd_work_priority`.
- **No collision**: confirm that registering `work` does NOT orphan `task` (both resolve). This is the spec §5.1 invariant.
- **All work commands use `payload_free=True`**: verify via `registry.is_payload_free(name)` for each of the 9 names.
- **`/work` is canonical in help output**: `/help` lists `/work`; `/help work` returns its help text.
- **No `aliases=` used**: grep the registration block — `aliases=` must NOT appear for any work command.

Update any existing test that relied on the old `TaskHandler.cmd_task`/`cmd_done`/etc. method names — they now point at `WorkHandler.cmd_work`/`cmd_work_done`/etc. Keep a fake handler in the test that has the `cmd_work*` methods.

Run `python3 -m pytest tests/test_command_handler.py tests/test_work_handler.py tests/test_command_models.py -v` and paste full output.

## Rules
- `prompts/steelFramedCodeWriter.md` — start with Discovery block.
- Verify no `aliases=` for work commands: `grep -n 'aliases=' ui/handlers/command_handler.py` — if any work command uses it, that's a bug.
- Do NOT modify `ui/window.py` (Phase 5), `ui/handlers/work_handler.py` (Phase 3), or any Phase 1/2 file.
- Do NOT rename the constructor param (Option A above).
- Existing non-task command wiring (help, ask, delegate, review, status, agents, cost, clear, compact, session) must remain unchanged.

## COMPLETENESS checklist (mandatory)

```
COMPLETENESS:
- [x] Edit 1: command_handler.py registration block replaced (9 canonical registrations, no aliases=) — evidence: <grep -c 'aliases=' in the work block = 0>, <grep -c 'register_command' count>
- [x] Edit 2: all 9 registrations use payload_free=True — evidence: <grep output>
- [x] Edit 3: /work and /task both route to cmd_work (no orphan) — evidence: <test name + output>
- [x] Edit 4: each legacy name routes to its cmd_work_* method — evidence: <test names + output>
- [x] Edit 5: /work is canonical in help — evidence: <test name + output>
- [x] Edit 6: existing non-task command wiring unchanged — evidence: <git diff scope>
- [x] Edit 7: window.py NOT modified — evidence: <git status shows window.py clean>
```

Report: the diff, new/updated test names, full pytest output, grep outputs. Flag related issues, don't silently fix. Write when done.
