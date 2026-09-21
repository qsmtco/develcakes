# Phase 6 of 8 — ProjectHandler: auto-add metadata, save_members backfill, created callback

**Master spec:** `docs/specs/SPEC-SUPERVISOR-ONBOARDING-REFINEMENTS.md` §2.6 + §2.10.

**Goal:** Four coupled edits in `ui/handlers/project_handler.py` (all spec §2.6):
1. `_auto_add_onboarding_agents()` — construct members from the agent definition (no hardcoded role/can_write).
2. `_save_members()` — backfill metadata for unknown `special:*` keys via the registry; remove the implicit roster git commit; refresh awareness snapshot after team save.
3. Add `set_on_project_created(cb)` callback API + `_on_project_created` slot.
4. `create_project()` fires the created callback after `open_project()`.

## Rules
- Use the `prompts/steelFramedCodeWriter.md` prompt. Read `ui/handlers/project_handler.py` in FULL before editing. Read `agent/special_agents.py` (`get_special_agent`) and `models/team.py` (`TeamMember`) too.
- Anchor edits to identifiers, not line numbers.
- Verify every claim with evidence (paste command output). This is a handler — GTK-free, but multi-edit. Be careful.

## Edit 1 — `_auto_add_onboarding_agents()`: use agent_def metadata

The current code hardcodes `role="onboarding guide"` and `can_write=True`. Per master spec §2.6, construct the new member from the definition:

```python
team.add_member(TeamMember(
    session_key=agent_def.conv_id_prefix,
    name=agent_def.display_name,
    role=agent_def.role,
    can_write=agent_def.can_write,
))
```

Keep everything else in the method unchanged: the `get_project_onboarding_agents()` lookup, the membership/routing behavior, the non-fatal `try/except` around the registry import, the `if not onboarding_agents or not self._awareness: return` guard, the routing-table add when project is active, and the `save_team` + log on change.

Do NOT hardcode `"onboarding guide"` or `True` anymore. Note: since Phase 1 flipped Auxilium's `auto_add_to_projects` to false and Supervisor has it false too, `get_project_onboarding_agents()` returns an empty list for current defaults — so this method is effectively a no-op for built-ins now. That's correct per spec §3 "a no-op for current defaults". The method must still work correctly for any legacy/opt-in onboarding definition.

## Edit 2 — `_save_members()`: registry backfill + no implicit commit + awareness refresh

Rewrite the awareness-path branch per master spec §2.6. The current code creates unknown members with only `name=""`. The new behavior:

For each `sk` in `members`:
- If `team.get_member(sk)` is not None → preserve that existing `TeamMember` exactly (append to new_members). **Order must equal the input `members` list order.**
- Else if `sk.startswith("special:")` → call `get_special_agent(sk)`. If it returns a definition, append `TeamMember(session_key=sk, name=agent_def.display_name, role=agent_def.role, can_write=agent_def.can_write)`. If it returns `None`, log a warning (`special agent not in registry: {sk}`) and append `TeamMember(session_key=sk, name="", role="", can_write=False)`.
- Else (gateway/unknown `agent:...` keys) → append `TeamMember(session_key=sk, name="", role="", can_write=False)`.

Import `get_special_agent` lazily (inside the method, alongside the existing `from models.team import TeamMember`). Spec §2.6: "Import `get_special_agent` lazily with the other local registry imports."

Then:
- **Remove** the line `self._git_commit_if_available(path, "update team roster")` from this method (spec §2.6 explicit). Explicit project-create and review-checkpoint commits elsewhere remain unchanged.
- **After** `save_team(path, team)`, refresh the awareness snapshot (best-effort, non-fatal): `snapshot = self._awareness.build_awareness_snapshot(path)` then `self._awareness.save_awareness_snapshot(path, snapshot)`. Guard it so a snapshot failure does not prevent the roster write from having succeeded — wrap in try/except and log consistently with existing non-fatal operations.

Preserve the legacy fallback (`if self._projects: self._projects.save_members(...)`) when awareness is absent — spec §2.6: "If the handler's awareness dependency is absent, preserve the legacy fallback and do not invoke awareness-only APIs." Do NOT call awareness snapshot APIs in the legacy branch.

## Edit 3 — Add `set_on_project_created` callback API

Near the other `set_on_*` methods (search for `def set_on_project_opened`), add:

```python
def set_on_project_created(self, cb: Callable[[str, str], None]) -> None:
    """Register a callback fired after a project is CREATED (not just opened).

    cb(name: str, path: str). Wired by ui/window.py for the System bubble.
    The callback is invoked AFTER open_project() completes, through the
    handler's GLib.idle_add dispatch so tab creation has completed before
    the callback body runs. Only fires for successful create_project(),
    not for open_project() of an existing project.
    """
    self._on_project_created = cb
```

And add the slot initialization in `__init__` alongside the other `_on_project_*` slots (search for `self._on_project_opened`). Add `self._on_project_created: Callable[[str, str], None] | None = None`.

## Edit 4 — `create_project()` fires the created callback

At the end of `create_project()`, AFTER `self.open_project(name, path)` returns and BEFORE `return path`, invoke the callback through the handler's `_dispatch` (GLib dispatch) so it runs on the main thread after tab creation:

```python
        # Open the project (creates tab, refreshes agents, fires opened callbacks)
        self.open_project(name, path)

        # SOR §2.7: notify the composition root AFTER open_project completes,
        # dispatched via GLib.idle_add so the project tab exists before the
        # callback body resolves the chat box.
        if self._on_project_created is not None:
            _cb = self._on_project_created
            self._dispatch(lambda: _cb(name, path))

        return path
```

Confirm `_dispatch` exists and routes through `GLib.idle_add` when GLib is provided (read the `_dispatch` method). This is the existing pattern — use it. The `lambda: _cb(name, path)` closes over the local `name`/`path` and the captured `_cb` to avoid late-binding surprises.

**Critical:** The callback must fire ONLY for `create_project`, never for `open_project` of an existing project. Do NOT add this dispatch to `open_project`.

## Edit 5 — Tests in `tests/test_project_handler.py`

Read `tests/test_project_handler.py` FIRST. Note its fixtures use a `FakeProjects` + `FakeGLib` + MagicMock `lp` pattern WITHOUT an awareness_module by default. For tests that need the awareness path, you must construct a handler with a real or fake awareness module. Look at `tests/test_create_project.py` (which DOES pass `awareness_module=pa`) for the pattern — use real `utils.project_awareness` against a temp project dir.

Add tests covering (spec §2.10):

1. **`_auto_add_onboarding_agents` uses registry metadata** — Set up a fake onboarding agent (monkeypatch `get_project_onboarding_agents` to return a `SpecialAgentDef` with specific `display_name`/`role`/`can_write`). Call the method. Assert the added `TeamMember` has the def's role and can_write (NOT the hardcoded "onboarding guide"/True).
2. **`_save_members` backfills special:* metadata** — Pre-create a project with an existing team. Monkeypatch `get_special_agent` to return a def for `special:supervisor`. Call `_save_members` with a members list containing `special:supervisor` (new). Assert the new member has the def's display_name/role/can_write.
3. **`_save_members` blank for gateway keys** — Call `_save_members` with an `agent:foo:telegram:direct:123` key (new). Assert the new member has `name=""`, `role=""`, `can_write=False`.
4. **`_save_members` preserves existing members** — Pre-create a team member with custom metadata. Call `_save_members` with that key in the list. Assert the existing member is preserved EXACTLY (custom name/role/can_write unchanged).
5. **`_save_members` does NOT git commit** — Spy on `_git_commit_if_available` (or `stage_all`/`commit`). Call `_save_members`. Assert NO commit was made for "update team roster". (Existing project-create commits elsewhere are unaffected — this test is scoped to `_save_members`.)
6. **`_save_members` refreshes awareness snapshot** — Spy on `save_awareness_snapshot`. Call `_save_members`. Assert `save_awareness_snapshot` was called once after `save_team`.
7. **`set_on_project_created` callback fires for create, not open** — Register a callback. Call `create_project` (with mocked filesystem/awareness so it succeeds). Assert the callback fired with `(name, path)`. Then call `open_project` on an existing project and assert the callback did NOT fire again.
8. **`set_on_project_created` callback deferred via GLib** — With `FakeGLib`, the callback should be queued (not run synchronously) until `dispatch_all()` is called. Assert the callback fires only after `dispatch_all()`.
9. **`_save_members` ordering equals input** — Pass members in a specific order including new + existing. Assert `team.get_session_keys()` returns that exact order.

For tests that need awareness, prefer a real `utils.project_awareness` against `tmp_path` (matching `test_create_project.py`'s pattern) over a MagicMock — it's more faithful and catches integration bugs. Each test must clean up / use isolated temp dirs.

## Verification (run and paste output)

```bash
# Removed patterns
grep -n 'role="onboarding guide"' ui/handlers/project_handler.py          # MUST return 0
grep -n 'can_write=True,  # needs write_file' ui/handlers/project_handler.py  # MUST return 0
grep -n '_git_commit_if_available(path, "update team roster")' ui/handlers/project_handler.py  # MUST return 0

# New patterns
grep -n 'def set_on_project_created' ui/handlers/project_handler.py        # must show the new method
grep -n '_on_project_created' ui/handlers/project_handler.py               # must show init + wiring + dispatch
grep -n 'agent_def.role\|agent_def.can_write\|agent_def.display_name' ui/handlers/project_handler.py  # must show the backfill

# Tests
XDG_CONFIG_HOME=/tmp/cctest_home/.config python3 -m pytest tests/test_project_handler.py tests/test_create_project.py -q 2>&1 | tail -10
```

## COMPLETENESS (mandatory)

```
COMPLETENESS:
- [ ] Edit 1: _auto_add_onboarding_agents uses agent_def role/can_write — evidence: grep 0 hardcoded patterns; test passes
- [ ] Edit 2: _save_members backfill (special via registry, gateway blank, existing preserved, ordered) — evidence: tests pass
- [ ] Edit 2: _save_members no implicit commit + awareness snapshot refresh — evidence: grep 0 roster-commit; test passes
- [ ] Edit 3: set_on_project_created callback API — evidence: grep def + __init__ slot
- [ ] Edit 4: create_project fires callback after open_project, deferred via GLib — evidence: test passes (create fires, open doesn't)
- [ ] Edit 5: 9 tests added — evidence: pytest output
- [ ] Any related issue found, not silently fixed (report here)
```
