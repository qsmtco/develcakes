# SPEC-12 SP2b — FIX ROUND (Debugger test-quality findings)

**Spec:** `docs/specs/SPEC-12-PROJECT-FIRST-GROUP-CHAT.md` §5/§6
**Base:** `docs/specs/phases/SPEC-12-SP2B-INSTRUCTIONS.md`
**Scope:** ONE test file — `tests/test_chat_render_handler.py`. No source edits.
RED-first (both fixes are about making a test that MUST fail under a reverted
source).

Auditor (Debugger) verified 6/7 rewritten handler tests genuinely discriminate
against a session-keyed revert, and found 2 gaps.

---

## BUG#1 (MEDIUM) — T3 is a tautological non-pin

`test_close_project_kills_agent_surface_mounted_there` PASSES even when
`_surface_for` is reverted to session-keyed caching, because its assertions are
(a) the absence of key `project:alpha` (never a cache key under the revert either)
and (b) a tombstone `close_session` sets unconditionally on its own argument.

**Fix — assert the POSITIVE identity + the absent per-session surface.** Rewrite
the body to:

```python
    def test_close_project_kills_the_project_surface(self, monkeypatch):
        """SPEC-12: the routed surface is CACHED under the project key, so
        closing the project destroys it and tombstones the project key —
        and leaves NO agent-keyed surface behind (the routed session does
        not get its own surface). Falsifier: a session-keyed cache → the
        pre-close `"project:alpha" in _surfaces` assert fails."""
        project_box = Gtk.Box()
        boxes = {"project:alpha": project_box}
        handler, created = self._wired(monkeypatch, boxes)
        handler.render_sync("Agent", "routed reply", "agent:sk",
                            mount_key="project:alpha")
        surface = created[0]
        assert surface.get_parent() is project_box
        # THE PIN (non-tautological): the surface lives UNDER THE PROJECT KEY
        # and the routed session did NOT create a session-keyed surface.
        assert handler._surfaces.get("project:alpha") is surface
        assert "agent:sk" not in handler._surfaces
        destroyed: list = []
        surface.destroy = lambda: destroyed.append(True)
        handler.close_session("project:alpha")
        assert destroyed == [True]
        assert "project:alpha" not in handler._surfaces
```

**Verify:** under a session-keyed revert (`_surface_for` keyed by `session_key`),
the new `assert handler._surfaces.get("project:alpha") is surface` FAILS. Paste
that RED proof.

## BUG#2 (LOW) — fan-out loop lost its dedicated coverage

`close_session`'s fan-out (destroy + tombstone every surface mounted in the
closed box) is now defensive-only: the display-key model collapses the normal
case to N=1, so the rewrite asserts `len(created)==1`. Removing the loop body
goes undetected.

**Fix — add a test that SEEDS a second surface into the box manually.** Append
after the T3 test:

```python
    def test_close_project_fans_out_over_extra_surfaces_in_box(self, monkeypatch):
        """SPEC-12 defensive path: close_session must destroy + tombstone
        EVERY surface mounted in the closed box, even when more than one is
        present (reachable when a direct session's getter key resolves to
        the same box). Seeded manually — the display-key model produces N=1
        by construction, so the loop needs an explicit stray. Falsifier:
        remove the fan-out loop body → the stray survives (assert fails)."""
        project_box = Gtk.Box()
        boxes = {"project:alpha": project_box}
        handler, created = self._wired(monkeypatch, boxes)
        # Primary project surface.
        handler.render_sync("Agent", "primary", "agent:sk",
                            mount_key="project:alpha")
        # Stray second surface mounted DIRECTLY in the same box, cached under
        # its own display key (the shape the loop defends against).
        stray = TextViewFallback()
        project_box.append(stray)
        handler._surfaces["agent:stray"] = stray
        handler._surfaces_by_parent[id(project_box)] = stray  # index consistency
        destroyed: list = []
        created[0].destroy = lambda: destroyed.append(id(created[0]))
        stray.destroy = lambda: destroyed.append(id(stray))
        handler.close_session("project:alpha", box=project_box)
        assert id(created[0]) in destroyed       # primary fanned out
        assert id(stray) in destroyed            # stray fanned out
        assert handler._surfaces == {}           # both drained
        assert handler._closed_sessions.get("project:alpha") is True
        assert handler._closed_sessions.get("agent:stray") is True
```

**Verify:** with the fan-out loop body removed, the `id(stray) in destroyed`
assert FAILS. Paste that RED proof (mutate/vcapture/restore).

## Do NOT
- Do not edit `ui/handlers/chat_render_handler.py` (source frozen; SP2a audited clean).
- Do not change `test_welcome_html.py` / `test_activity_pill_adapter.py`.
- Do not rename other tests.

## Verification battery (paste all outputs)
- `cd /home/mushy/projects/develcakes && xvfb-run -a .venv/bin/python -m pytest tests/test_chat_render_handler.py tests/test_welcome_html.py tests/test_activity_pill_adapter.py -q` → ALL GREEN
- RED proofs: (a) session-keyed revert → new T3 assert fails; (b) loop-body removal → stray assert fails
- `~/.local/bin/ruff check tests/test_chat_render_handler.py` → 0

## Report back (mandatory COMPLETENESS)
```
COMPLETENESS:
- [x/not done] BUG#1: T3 rewritten to positive-identity pin — evidence: diff hunk
- [x/not done] BUG#1 RED proof: session-keyed revert fails the new assert — pasted
- [x/not done] BUG#2: fan-out stray-surface test added — evidence: diff hunk
- [x/not done] BUG#2 RED proof: loop removal fails the stray assert — pasted
- [x/not done] full battery + ruff — pasted
```

Invoke `prompts/steelFramedCodeWriter.md` before writing. Please write the fixes
per this brief and report when done.