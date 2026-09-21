# Phase 7 of 8 — Wire the project-created System bubble in ui/window.py

**Master spec:** `docs/specs/SPEC-SUPERVISOR-ONBOARDING-REFINEMENTS.md` §2.7 + §2.10.
**Prerequisite:** Phase 6 added `ProjectHandler.set_on_project_created()` + the `_on_project_created` slot + the `create_project()` dispatch. This phase wires the composition-root side.

**Goal:** Register a `_on_project_created_system_bubble` callback on the ProjectHandler at composition time, and implement that method on `MainWindow` so it renders a System bubble in the new project's chat tab telling the user to add Supervisor manually.

## Rules
- Use the `prompts/steelFramedCodeWriter.md` prompt. Read `ui/window.py` in FULL before editing — it is large (~1569 lines). Pay attention to: where `set_on_project_opened` / `set_on_project_closed` are wired (the existing pattern to copy); the `_on_project_opened` / `_on_project_closed` method definitions (style to mirror); and the existing `_dispatch` / `GLib.idle_add` usage.
- Anchor edits to identifiers, not line numbers.
- Verify every claim with evidence (paste command output). GTK-dependent tests may segfault in this sandbox — if so, report it as environmental and rely on static grep verification + the unit-test coverage from Phase 6.

## Edit 1 — Register the callback at composition time

Near the existing `self._project_handler.set_on_project_opened(...)` / `set_on_project_closed(...)` wiring (search for `set_on_project_opened`), add:

```python
self._project_handler.set_on_project_created(self._on_project_created_system_bubble)
```

Place it alongside the other project-lifecycle callback registrations (NOT inside a lambda — register the bound method). Use the EXACT callback name `_on_project_created_system_bubble` so the spec's verification grep (§10) matches unambiguously.

## Edit 2 — Implement `_on_project_created_system_bubble` on `MainWindow`

Add a new method to `MainWindow` (near `_on_project_opened` / `_on_project_closed` — mirror their style and docstring convention). The method must defer its widget work through `GLib.idle_add` (or the existing equivalent main-loop dispatch) for main-thread safety, because `open_project()` and downstream tab creation can be deferred through `GLib.idle_add`.

Per master spec §2.7, inside the deferred callback execute this EXACT sequence:

1. Derive `session_key = f"project:{name}"`.
2. Call `chat_box = self._main_content.get_chat_box_for_session(session_key)`.
3. If `chat_box is None`, call `self._main_content.create_chat_tab(session_key, "System")` first, then call `get_chat_box_for_session(session_key)` again.
4. If the chat box is still unavailable, return without rendering (no-op safely).
5. Build the exact message text (Edit 3 below).
6. Call `bubble = self._chat_render_handler.render_sync("System", text, session_key, tab_key=session_key)` using the verified signature.
7. Append the returned bubble only when it is not `None`.
8. Call `self._main_content.scroll_chat_to_bottom()` after append.

Structure:

```python
def _on_project_created_system_bubble(self, name: str, path: str) -> None:
    """Render the project-created System bubble (composition-root side).

    SOR §2.7: fired by ProjectHandler.set_on_project_created AFTER
    open_project completes (dispatched via the handler's GLib.idle_add).
    This method defers its widget work through GLib.idle_add so the
    project tab exists before the callback body resolves the chat box.
    Create-only — never fires for open_project of an existing project.
    """
    def _deferred():
        session_key = f"project:{name}"
        chat_box = self._main_content.get_chat_box_for_session(session_key)
        if chat_box is None:
            self._main_content.create_chat_tab(session_key, "System")
            chat_box = self._main_content.get_chat_box_for_session(session_key)
        if chat_box is None:
            return False  # chat box unavailable — no-op safely
        text = (
            f"New project '{name}' created. Add the Supervisor agent from the "
            f"Agents tab (click the +), then send it a message like "
            f"'I'm ready' to begin onboarding."
        )
        bubble = self._chat_render_handler.render_sync(
            "System", text, session_key, tab_key=session_key
        )
        if bubble is not None:
            chat_box.append(bubble)
        self._main_content.scroll_chat_to_bottom()
        return False  # don't repeat
    GLib.idle_add(_deferred)
```

**Confirm** before writing: how does `MainWindow` reference GLib? Check the top-of-file imports (search for `from gi.repository import` / `import gi`). Use the same name the rest of `_build` uses. If `_dispatch`-style helpers exist, match the pattern. Read `_on_project_opened` and `_on_project_closed` to see how THEY schedule deferred work (they may use `GLib.idle_add` directly or a helper) and mirror that exactly.

**Confirm** that `chat_box.append(bubble)` is the correct append API — read `ui/views/main_content.py` around `get_chat_box_for_session` to see what kind of object it returns (likely a `Gtk.Box`), and check how existing code appends bubbles to it. If the existing system-message code path uses a different append call, match it.

## Edit 3 — Exact user-facing message

The message text must be EXACTLY (with `{name}` substituted):

> `New project '<name>' created. Add the Supervisor agent from the Agents tab (click the +), then send it a message like 'I'm ready' to begin onboarding.`

Use an f-string with curly quotes preserved. The apostrophe in `I'm` is a literal ASCII apostrophe. Verify by grepping the rendered string after the edit.

## Edit 4 — Static test (tests/test_window_*.py or new file)

window.py is hard to unit-test (requires GTK). Add a STATIC regression test that does NOT instantiate GTK: assert the source contains the required wiring. Look at `tests/test_architecture.py` (which does static source checks) for the pattern. Add:

```python
def test_project_created_system_bubble_wired():
    """SOR §2.7: MainWindow wires set_on_project_created to a named handler."""
    import pathlib
    src = pathlib.Path("ui/window.py").read_text()
    assert "set_on_project_created(self._on_project_created_system_bubble)" in src
    assert "def _on_project_created_system_bubble" in src
    assert "New project '" in src  # exact message prefix
    assert "Add the Supervisor agent from the" in src
```

Place it in the appropriate test file (check `tests/test_architecture.py` first — if it already does static window.py source checks, add there; else create `tests/test_window_project_created.py`). Use `pathlib.Path` relative to the repo root (pytest runs from repo root). This test runs without GTK and guards against the wiring being accidentally removed.

## Verification (run and paste output)

```bash
# Wiring present
grep -n "set_on_project_created(self._on_project_created_system_bubble)" ui/window.py
grep -n "def _on_project_created_system_bubble" ui/window.py
grep -n "New project '" ui/window.py  # exact message prefix

# Exact callback name grep per spec §10 — each must return exactly one declaration + one wiring + test refs
grep -rn "set_on_project_created" --include="*.py" .
grep -rn "_on_project_created_system_bubble" --include="*.py" .

# Static test passes (no GTK needed)
XDG_CONFIG_HOME=/tmp/cctest_home/.config python3 -m pytest <your-test-file> -q 2>&1 | tail -8

# Phase 6 handler tests still pass (regression — callback contract unchanged)
XDG_CONFIG_HOME=/tmp/cctest_home/.config python3 -m pytest tests/test_project_handler.py -q 2>&1 | tail -5
```

## COMPLETENESS (mandatory)

```
COMPLETENESS:
- [ ] Edit 1: set_on_project_created(self._on_project_created_system_bubble) registered at composition time — evidence: grep output
- [ ] Edit 2: _on_project_created_system_bubble method added, defers via GLib.idle_add, exact 8-step sequence — evidence: grep + method source
- [ ] Edit 3: exact message text present — evidence: grep "New project '" + "Add the Supervisor agent from the"
- [ ] Edit 4: static regression test added — evidence: pytest pass
- [ ] Spec §10 callback greps each return: 1 declaration + 1 wiring + test refs — evidence: grep -rn output
- [ ] Phase 6 handler regression: test_project_handler.py still passes — evidence: pytest
- [ ] Any related issue found, not silently fixed (report here)
```
