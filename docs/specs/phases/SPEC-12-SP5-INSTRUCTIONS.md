# SPEC-12 SP5 — window.py: remove auto-open; Chat → project tab

**Spec:** `docs/specs/SPEC-12-PROJECT-FIRST-GROUP-CHAT.md` §2d (rule R3)
**Pre-flight:** `docs/specs/phases/SPEC-12-PREFLIGHT-DECISIONS.md`
**Supervisor:** Supervisor; **Builder:** Coder; **Auditor:** Debugger
**Scope:** ONE file — `ui/window.py` (+ any test that asserts the auto-open/tab-click
behavior). Read the file's `_build` and `_on_agent_selected` first.

---

## Edit 1 — delete the auto-open block (~:196-207) + its import

Delete the whole block:
```python
        # Phase 4 — Auto-open agent tabs on every launch.
        # Creates a tab for each agent with auto_open=True.
        auto_open_agents = get_auto_open_agents()
        if auto_open_agents:
            for agent_def in auto_open_agents:
                self._main_content.create_chat_tab(
                    agent_def.conv_id_prefix, agent_def.display_name
                )
                logger.info(
                    "Auto-opened agent tab: %s",
                    agent_def.display_name,
                )
```
And change the import line back to its used form (F401 guard):
```python
        from agent.special_agents import get_special_agents
```
(Verify `get_auto_open_agents` has no other use in the file first —
`grep -n get_auto_open_agents ui/window.py` must be 0 after.)

**Replacement is a NO-OP** (no launch-time project auto-open — an earlier draft
invented one; `ProjectHandler._active_project_name` starts `None`, so there is
nothing to auto-open at launch).

## Edit 2 — `_on_agent_selected` opens the PROJECT tab (R3)

Current:
```python
    def _on_agent_selected(self, session_key, agent_name):
        """Called when an agent row is clicked — create/open chat tab."""
        self._main_content.create_chat_tab(session_key, agent_name)
```

New (R3: member → project tab; non-member → no-op; the member toggle `+` adds):
```python
    def _on_agent_selected(self, session_key, agent_name):
        """Agent-list "Chat" click (SPEC-12 R3): open the ACTIVE project's
        group tab when the agent is a member; otherwise no-op (the member
        toggle '+' is the path to add it). The private /ask view is a command
        path, not this button."""
        project_name = None
        if self._project_handler is not None:
            project_name = self._project_handler.get_active_project_name()
        if project_name is None:
            return
        members = self._project_handler.get_project_members(project_name)
        if session_key not in members:
            return
        self._main_content.create_chat_tab(
            f"project:{project_name}", project_name)
```

**Guard:** `self._project_handler` is assigned later in `_build` than the
agent-list handler wiring — confirm it exists by the time `_on_agent_selected`
can fire (it is a click handler, so it runs post-build; `getattr`-guard or rely
on the attribute). If `_project_handler` may be absent, use
`getattr(self, "_project_handler", None)`.

## What must NOT change
- The special-agent registration loop (`get_special_agents`).
- Any other window method.
- The private-view command path (chat_handler — already SPEC-12'd).

## Tests
- **Pre-flight verified: NO existing test references `auto_open`,
  `_on_agent_selected`, or `get_auto_open_agents`** (`grep -rn` over `tests/` → 0).
  So no test rewrites are strictly required; add a new focused test if a
  window-test harness exists, else note the coverage gap.
- Add a small test (or extend `tests/test_window*.py` if present) pinning:
  member click → `create_chat_tab("project:alpha", "alpha")`; non-member click →
  NOT called; no active project → NOT called. If no window harness exists, use a
  handler-level unit (construct the shell / call `_on_agent_selected` with a
  stubbed `_project_handler` + `_main_content`).

## Verification (paste outputs)
- `cd /home/mushy/projects/develcakes && xvfb-run -a .venv/bin/python -m pytest tests/ -q -k "window or agent_selected or auto_open"` → paste
- Full `tests/test_window*.py` if present → paste
- `grep -c get_auto_open_agents ui/window.py` → 0
- `~/.local/bin/ruff check ui/window.py` → vs HEAD baseline
- `.venv/bin/pyright ui/window.py` → vs HEAD

## Report back (mandatory COMPLETENESS)
```
COMPLETENESS:
- [x/not done] Edit 1: auto-open + import removed; grep 0 — evidence: diff + grep
- [x/not done] Edit 2: _on_agent_selected → project tab (R3) — evidence: diff hunk
- [x/not done] tests updated/added — evidence: pytest
- [x/not done] ruff / pyright vs HEAD — pasted
- [x/not done] Related issues found, NOT fixed
```

Invoke `prompts/steelFramedCodeWriter.md` before writing. Please write the change per
this brief and report when done.