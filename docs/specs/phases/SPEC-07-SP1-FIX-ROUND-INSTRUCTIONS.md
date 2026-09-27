# SPEC-07 SP1 FIX ROUND — audit findings BUG #1 + BUG #2 (Debugger, 2026-09-26)

**Builder rule:** `prompts/steelFramedCodeWriter.md` — invoke it, apply every rule.
**Context:** SP1 landed clean per brief, but the adversarial audit found one HIGH
(my brief specified the wrong stylesheet target — copying a latent SPEC-06 mismatch)
and one LOW (false test-env claim). This round fixes both. No other changes.

## BUG #1 (HIGH) — pill classes have no GTK stylesheet rules

The pill `Gtk.Label` carries `pill-*` CSS classes, but the rules live in `_BASE_CSS`,
which is ONLY injected into the WebKit HTML document (chat_surface.py:127). No GTK
provider has any `.pill-*` rule (`grep -n pill ui/styles.py` → zero). Every state
renders theme-default — a color-blind pill that regresses the colored bar it replaces.
Latent since SPEC-06: the pill had zero callers, so nobody ever saw the colors.

**Fix (ruling (a)+(b) combined):**
1. Add to `ui/styles.py` `APP_CSS` (the GTK stylesheet, loaded at :1663 via
   `add_provider_for_display`) — same color values as the webview rules:

```css
.pill-idle { color: #6b6b7a; }
.pill-thinking { color: #e0af68; }
.pill-tool { color: #7aa2f7; }
.pill-error { color: #f7768e; }
.pill-streaming { color: #7dcfff; }
.pill-done { color: #9ece6a; }
```

   Place them in the same section style as neighboring rules (match APP_CSS's
   existing formatting/indentation conventions — read it first).

2. REMOVE the six `.pill-*` rules from `_BASE_CSS` in chat_surface.py (:101-106 area).
   The webview document never renders a pill element — dead CSS. (`tok-*`/`lang-*`
   etc. stay; only pill rules move.)

3. Repoint `tests/test_activity_pill_adapter.py::test_surface_state_map`'s second
   assertion: it currently checks `f".{cls} {{" in _BASE_CSS` (wrong stylesheet —
   the test passed while the widget was unstyled). Change it to import `APP_CSS`
   from `ui.styles` and assert the rule presence there. Keep the map-equality
   assertion unchanged.

4. NEW pin (this is the tooth the old assertion lacked): a test that APP_CSS
   actually contains a usable rule for every pill class. `apply_styles()`
   early-returns headless (`display is None` — verified at ui/styles.py:1654-1658),
   so effective-color observation is not feasible in CI; the pin parses `APP_CSS`
   text and asserts every value in `_ACTIVITY_STATE_TO_CSS.values()` has a rule
   `.pill-<state> { color: …; }` with a NON-EMPTY color value, and that the 6
   distinct classes have DISTINCT colors (the map deliberately shares
   sending≡thinking; the other 6 must not collide). This test MUST be able to
   fail: delete a rule from APP_CSS → red; duplicate a color where the map
   expects distinct → red.

## BUG #2 (LOW) — false docstring claim about xvfb

`tests/test_activity_pill_adapter.py` header says the whole file runs bare (no display).
Test 8 (`test_surface_for_key_readonly`) builds a real TextViewFallback — real GTK.
`env -u DISPLAY` → segfault. Fix: convert test 8 to a pure fake. Replace the
`SpySurface(TextViewFallback)` with a minimal registered fake:

```python
class RegisteredFakeSurface:
    """Pure fake standing in for a surface entry — no gi, no display."""
    def __init__(self):
        self.appended = []
    def append_message(self, role, html_fragment, agent_name=None):
        self.appended.append({"role": role, "html": html_fragment, "agent_name": agent_name})
```

Monkeypatch `create_chat_surface` to produce it (same monkeypatch seam test 8 already
uses), call `handler.render_sync("Agent", "hello", "real-key")` — the render path needs
the surface to accept `append_message`; the pure fake records the same evidence. Then:
- remove the `xvfb` mention for test 8; the header claim "runs bare, no xvfb" becomes TRUE.
- verify: `env -u DISPLAY .venv/bin/python -m pytest tests/test_activity_pill_adapter.py -q`
  must pass. Paste the output.
- Keep the ChatRenderHandler import — it is pure-Python (no gi)? VERIFY first: if
  `ChatRenderHandler` imports gi transitively, either keep the import lazy inside the
  test function, or keep xvfb for the file and fix ONLY the comment. Verify, then choose;
  report which you did.

## Verification (paste full output)

```
xvfb-run -a .venv/bin/python -m pytest tests/test_activity_pill_adapter.py tests/test_chat_surface.py tests/test_chat_render_handler.py -v
env -u DISPLAY .venv/bin/python -m pytest tests/test_activity_pill_adapter.py -q   # BUG #2 proof
grep -n "pill" ui/styles.py                                                        # 6 rules present
grep -n "pill" ui/views/chat_surface.py                                            # ZERO _BASE_CSS rules remain
python -m ruff check ui/styles.py ui/views/chat_surface.py tests/test_activity_pill_adapter.py
```

## COMPLETENESS (mandatory)

- [ ] BUG #1: APP_CSS gains 6 pill rules (grep pasted)
- [ ] BUG #1: _BASE_CSS pill rules removed (grep → 0 in chat_surface.py)
- [ ] BUG #1: test_surface_state_map repointed to APP_CSS
- [ ] BUG #1: new color-tooth pin (can-fail proof pasted — delete-a-rule red run)
- [ ] BUG #2: test 8 pure-fake OR comment-only fix (report which + env -u DISPLAY result)
- [ ] Regression green + ruff clean (pasted)
- Related issues found, not fixed: <list or none>

Deviations carry a one-sentence rationale each.
