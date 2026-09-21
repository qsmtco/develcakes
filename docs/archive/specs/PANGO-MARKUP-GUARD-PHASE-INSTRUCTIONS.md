# Phase PANGO-MARKUP-GUARD — Phase Instructions

**Feature:** Fix the `Gtk-WARNING: Failed to set text` terminal warning and the 3 red tests from the settings-bar label refactor.
**Supervisor:** Supervisor
**Builder:** Coder
**Auditor:** Debugger

## Background

A master report at `docs/specs/SPEC-SETTINGS-BAR-TERMINAL-MASTER-REPORT.md` documents two issues:

1. **S1 (issue):** `make_safe_label` in `utils/gtk_safe_link.py` calls `label.set_markup(markup)` with NO try/except guard. When the markup contains source-code fragments with Pango-looking tags that got asymmetrically escaped by `escape_for_pango` (balanced `<b></b>` preserved, orphan `<span>` escaped), Pango rejects the whole string. GTK emits `Gtk-WARNING: Failed to set text '...' from markup due to error parsing markup` to the terminal and renders an **empty label**. The user sees a blank/truncated bubble.

2. **F5 (test regression):** The settings-bar label refactor (adding `Chat:`/`Files:`/`Git:` prefixes via child `Gtk.Label` widgets) changed the agent/auto-accept buttons from `Gtk.Button(label=...)` to `Gtk.Button()` + `set_child(_label)`. The test fake `_FakeButton` in `tests/test_main_content_settings_bar.py` does not implement `set_child()`. Three tests now fail with `AttributeError: '_FakeButton' object has no attribute 'set_child'`.

## Fixes Required

### Fix 1 — Guard `set_markup` in `make_safe_label` (`utils/gtk_safe_link.py`)

**File:** `utils/gtk_safe_link.py`, function `make_safe_label` (the line containing `label.set_markup(markup)`).

**Change:** Wrap the `label.set_markup(markup)` call in a try/except. On Pango parse failure (a `GLib.Error`), fall back to `label.set_text(markup)` so the raw text is shown (with literal tags visible, not an empty bubble) and NO terminal warning is emitted.

**Requirements:**
- Catch the exception that GTK raises. `Gtk.Label.set_markup()` does NOT raise a Python exception by default — it emits a `Gtk-WARNING` via GLib's log handler and returns without setting the text. To actually CATCH this, you must use `Pango.parse_markup()` to pre-validate the markup BEFORE calling `set_markup`. If `Pango.parse_markup()` fails, fall back to `set_text(markup)`.
- Import `Pango` (it is already imported in this file: `from gi.repository import Gtk, Pango`).
- Log the parse failure at DEBUG level via `logger.debug(...)` (the `logger` already exists at module level). Do NOT log at WARNING/ERROR — this is expected for adversarial input, and the fallback is the correct behavior. Logging at WARNING would spam the terminal.
- The fallback `set_text(markup)` shows the raw markup string (including literal `<b>` etc.). This is the correct degraded behavior: the user sees the text rather than an empty bubble.
- Do NOT change any other line in the function.

**Pseudocode for the guarded block:**
```python
# Pre-validate markup before set_markup to avoid Gtk-WARNING terminal spam.
# If Pango rejects the markup (asymmetric escaping from escape_for_pango),
# fall back to set_text so the raw text is visible instead of an empty bubble.
try:
    Pango.parse_markup(markup, -1, "\x00")
    label.set_markup(markup)
except Exception:
    logger.debug("Pango markup rejected, falling back to set_text: %r", markup[:120])
    label.set_text(markup)
```

**Rationale for `Pango.parse_markup` pre-validation over catching GLib signals:** `Gtk.Label.set_markup()` does not raise a catchable Python exception on malformed markup — it logs a `Gtk-WARNING` via `g_log` and silently leaves the label empty. The only way to prevent the terminal warning is to validate BEFORE calling `set_markup`. `Pango.parse_markup()` raises a `GLib.Error` (a subclass of Python `Exception`) on malformed markup, which IS catchable.

### Fix 2 — Add `set_child` to `_FakeButton` (`tests/test_main_content_settings_bar.py`)

**File:** `tests/test_main_content_settings_bar.py`, class `_FakeButton`.

**Change:** Add a `set_child(self, widget)` method that stores the child widget (mirroring `Gtk.Button.set_child()`). The `_FakeButton` class inherits from `_FakeBox` which already has `append`/`children`. Add:

```python
def set_child(self, widget):
    self._child = widget
```

Store it on an attribute so tests can inspect it if needed. Initialize `self._child = None` in `__init__` (call `super().__init__()` first).

## Acceptance Criteria

1. `utils/gtk_safe_link.py` `make_safe_label` pre-validates markup via `Pango.parse_markup` before `set_markup`, falling back to `set_text` on failure.
2. No `Gtk-WARNING` is emitted when malformed markup (e.g. `<span>&lt;b&gt;x</b>`) is passed to `make_safe_label`.
3. `tests/test_main_content_settings_bar.py` `_FakeButton` implements `set_child()`.
4. All 3 currently-failing tests pass: `test_update_project_settings_shows_on_nonempty`, `test_xml_escape_for_project_name`, `test_xml_escape_for_branch`.
5. No existing tests are broken.

## Files in Scope

- `utils/gtk_safe_link.py` (Fix 1 — the `make_safe_label` function only)
- `tests/test_main_content_settings_bar.py` (Fix 2 — the `_FakeButton` class only)

## Verification Commands

```bash
# Fix 1: confirm the guard exists
grep -n "Pango.parse_markup\|set_text(markup)" utils/gtk_safe_link.py

# Fix 2: confirm set_child exists on _FakeButton
grep -n "def set_child" tests/test_main_content_settings_bar.py

# The 3 previously-failing tests now pass
python3 -m pytest tests/test_main_content_settings_bar.py -q

# No regressions in related test suites
python3 -m pytest tests/test_markdown.py tests/test_escaping.py -q
```

## Out of Scope

- The `escape_for_pango` stack-based asymmetry (S2 in the master report) — that's a deeper design issue, deferred to a future spec.
- The settings bar production code (`ui/views/main_content.py`) — already correct, no changes.
- The `CRABCAKES_TEXTVIEW_BUBBLES` flag path (`chat/renderer.py`) — out of scope; the fix here covers the Pango-label path only.
