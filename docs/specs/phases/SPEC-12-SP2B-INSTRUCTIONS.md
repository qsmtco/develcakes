# SPEC-12 SP2b — rewrite the 3 test files to the display-key model

**Spec:** `docs/specs/SPEC-12-PROJECT-FIRST-GROUP-CHAT.md` §2b, §5, AC
**Pre-flight:** `docs/specs/phases/SPEC-12-PREFLIGHT-DECISIONS.md`
**Base:** `docs/specs/phases/SPEC-12-SP2A-INSTRUCTIONS.md` (source already done)
**Supervisor:** Supervisor; **Builder:** Coder; **Auditor:** Debugger
**Scope:** THREE test files ONLY — no source edits (SP2a landed the source).
1. `tests/test_chat_render_handler.py` (7 tests)
2. `tests/test_activity_pill_adapter.py` (1 test)
3. `tests/test_welcome_html.py` (verify only — expected already green)

Read each file before editing. RED-first is inherent here (the source is already
changed, so these assertions are already failing) — your job is to update each
assertion to the NEW contract and confirm the suite goes green.

## The model (why these change)

`_surfaces` is now keyed by **display key** = `mount_key or session_key`. A project
tab `project:<name>` has ONE surface. `MainContent.create_chat_tab("project:<name>")`
calls `render_welcome("project:<name>")`, which EAGERLY creates+mounts that ONE
surface. An agent reply rendered with `mount_key="project:<name>"` therefore lands in
the SAME surface (no second creation). Consequence for the tests: the old
"agent surface keyed `agent:sk`, mounted in the project box" expectations become
"one surface keyed `project:<name>`".

---

## File 1 — `tests/test_chat_render_handler.py`

### T1 · `TestSurfaceMountLifecycle::test_project_routed_reply_mounts_in_project_box`
Fake-box `_wired` (NO real tab → no welcome). New contract:
```python
        agent_box = Gtk.Box()
        project_box = Gtk.Box()
        boxes = {"project:alpha": project_box, "agent:sk": agent_box}
        handler, created = self._wired_handler(monkeypatch, boxes)
        handler.render_sync("Agent", "to project", "agent:sk",
                            mount_key="project:alpha")
        # ONE surface, keyed by the DISPLAY key (the project box).
        assert set(handler._surfaces) == {"project:alpha"}
        assert created[0].get_parent() is project_box
        assert agent_box.get_first_child() is None       # not in the agent box
        # personal reply (no mount_key) → own SESSION key
        handler.render_sync("Agent", "personal", "agent:other")
        assert "agent:other" in handler._surfaces
        assert handler._surfaces["agent:other"].get_parent() is None
        # same session, NO mount_key → a DISTINCT session-keyed surface
        handler.render_sync("Agent", "direct", "agent:sk", mount_key=None)
        assert "agent:sk" in handler._surfaces          # session key
        assert handler._surfaces["agent:sk"].get_parent() is None
        # the project surface is untouched by the personal/direct renders
        assert handler._surfaces["project:alpha"] is created[0]
        assert created[0].get_parent() is project_box
```

### T2 · `test_surface_cache_stays_session_keyed_with_mount_key`
**Rename** to `test_surface_cache_is_display_keyed`. New contract — ONE surface per
display key, DIFFERENT display keys → different surfaces:
```python
        boxes = {"project:alpha": Gtk.Box(), "project:beta": Gtk.Box()}
        handler, created = self._wired_handler(monkeypatch, boxes)
        handler.render_sync("Agent", "one", "agent:sk", mount_key="project:alpha")
        handler.render_sync("Agent", "two", "agent:sk", mount_key="project:beta")
        assert set(handler._surfaces) == {"project:alpha", "project:beta"}
        assert len(created) == 2                     # two display keys, 2 surfaces
        # Same display key, DIFFERENT session → SAME surface (the point).
        handler.render_sync("Agent", "three", "agent:other", mount_key="project:alpha")
        assert handler._surfaces["project:alpha"] is created[0]
        assert len(created) == 2
```

### T3 · `TestStreamingMountLifecycle::test_close_project_kills_agent_surface_mounted_there`
Now the surface key IS the project key. Rewrite:
```python
        project_box = Gtk.Box()
        boxes = {"project:alpha": project_box}
        handler, created = self._wired(monkeypatch, boxes)
        handler.render_sync("Agent", "routed reply", "agent:sk",
                            mount_key="project:alpha")
        surface = created[0]
        assert surface.get_parent() is project_box
        destroyed: list = []
        surface.destroy = lambda: destroyed.append(True)
        handler.close_session("project:alpha")
        assert destroyed == [True]
        assert "project:alpha" not in handler._surfaces
        assert handler._closed_sessions.get("project:alpha") is True
```

### T4 · `TestStreamingMountLifecycle::test_reopen_remounts_after_project_close`
Real tab path. New expected creation count = **2** (first-life project surface +
post-reopen fresh project surface; the agent render shares the project surface, and
`create_chat_tab`'s welcome creates the reopened one). Update the assertions:
- keep the "setter alone does NOT resurrect" check (`len(created) == 1` after the
  dropped render) — still valid;
- after `mc.create_chat_tab("project:alpha", "Alpha")` + the second-life render,
  assert `len(created) == 2` and that `created[1]` is mounted in the new box, with
  the comment: *"welcome created the reopened project surface (+1); the agent render
  shares it (display-keyed)"*. Drop the old `== 3` and its "agent surface" comment.

### T5 · `TestRound3Lifecycle::test_close_destroys_all_surfaces_in_project_box`
**Rename** to `test_close_destroys_the_project_surface`. Two agent renders with the
SAME mount_key now share ONE surface:
```python
        project_box = Gtk.Box()
        boxes = {"project:alpha": project_box}
        handler, created = self._wired(monkeypatch, boxes)
        handler.render_sync("Agent", "a", "agent:one", mount_key="project:alpha")
        handler.render_sync("Agent", "b", "agent:two", mount_key="project:alpha")
        assert len(created) == 1                      # ONE surface per project box
        destroyed: list = []
        created[0].destroy = lambda: destroyed.append(True)
        handler.close_session("project:alpha")
        assert destroyed == [True]
        assert handler._surfaces == {}
        assert handler._closed_sessions.get("project:alpha") is True
        assert id(project_box) not in handler._surfaces_by_parent   # FIX 6 kept
```

### T6 · `TestRound3Lifecycle::test_reopen_via_create_chat_tab_clears_tombstones`
Real tab path. New expected creation count = **2** (first-life project surface +
reopened project surface via welcome). Update:
- `assert len(handler._closed_sessions)` after close — still true (keyed
  `project:alpha` now);
- after reopen assert `not handler._closed_sessions`;
- `assert len(created) == 2` (drop `== 3`), comment: *"welcome re-created the
  reopened project surface; the agent render shares it"*.

### T7 · `TestCloseFanOutProductionPath::test_project_close_fans_out_and_reopen_remounts`
Real tabs. `create_chat_tab` already created the project surface via welcome, so the
agent render no longer adds one (`created[agent_before]` → IndexError today). Rewrite:
```python
            mc.create_chat_tab("project:alpha", "Alpha")
            old_box = mc.get_chat_box_for_session("project:alpha")
            handler.render_sync("Agent", "hello", "agent:x", mount_key="project:alpha")
            # ONE surface for the project box (welcome + agent rows share it).
            assert handler._surfaces["project:alpha"].get_parent() is old_box

            page = mc._find_page_by_session("project:alpha")
            assert page is not None
            mc._close_tab(page)
            assert mc.get_chat_box_for_session("project:alpha") is None
            assert "project:alpha" not in handler._surfaces
            assert handler._closed_sessions.get("project:alpha") is True

            mc.create_chat_tab("project:alpha", "Alpha")
            assert "project:alpha" not in handler._closed_sessions
            new_box = mc.get_chat_box_for_session("project:alpha")
            assert new_box is not old_box
            handler.render_sync("Agent", "back", "agent:x", mount_key="project:alpha")
            assert handler._surfaces["project:alpha"].get_parent() is new_box
```
Keep the test's docstring intent (the BUG#2 dead-fan-out falsifier), retargeted to the
display key.

## File 2 — `tests/test_activity_pill_adapter.py` (BUG#8, spec §2b)

`test_project_tab_resolver_picks_per_key` asserts the RETIRED agent-keyed model.
Rewrite it to the display-key contract and rename to
`test_project_tab_resolver_uses_display_key`:
```python
    handler.render_welcome("project:alpha")          # creates surface project:alpha
    handler.render_sync("Agent", "working the task", "agent:coder",
                        mount_key="project:alpha")    # SAME surface
    assert len(created) == 1                          # one display-keyed surface
    assert handler.surface_for_key("project:alpha") is created[0]
    assert handler.surface_for_key("agent:coder") is None   # retired model
```
Also update the module-level comment at `:207-209` ("surface_for_key is READ-ONLY")
only if it now misstates the key domain — keep the READ-ONLY semantics (unchanged).

## File 3 — `tests/test_welcome_html.py`

Expected ALREADY GREEN (its `_closed_sessions["sk"] = True` write at :192 has no
handler read-back). Verify it stays green; if any assertion now encodes the old model,
update it to the display key and note it. No edit unless a real failure appears.

---

## Verification battery (paste all outputs)
- `cd /home/mushy/projects/develcakes && xvfb-run -a .venv/bin/python -m pytest tests/test_chat_render_handler.py tests/test_welcome_html.py tests/test_activity_pill_adapter.py -q` → **ALL GREEN** (was 8 failed / 76 passed)
- `~/.local/bin/ruff check tests/test_chat_render_handler.py tests/test_activity_pill_adapter.py tests/test_welcome_html.py` → 0 (these test files are clean today)
- `wc -l` each test file

## Report back (mandatory COMPLETENESS)
```
COMPLETENESS:
- [x/not done] T1-T7: each test rewritten — evidence: diff hunks
- [x/not done] Activity-pill test rewritten — evidence: diff hunk
- [x/not done] welcome_html verified green — evidence: pytest
- [x/not done] Command 1 output (ALL GREEN) — pasted
- [x/not done] ruff output — pasted
- [x/not done] Related issues found, NOT fixed
```

Invoke `prompts/steelFramedCodeWriter.md` before writing. Please write the test
rewrites per this brief and report when done.