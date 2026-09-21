# Prompts Tab Right-Click Copy Menu — Tier-3 Follow-up Phase 1

## Purpose

Fix the two Tier-3 follow-ups documented in the post-mortem for the right-click copy menu feature:

1. **Popover leak on ESC/click-outside dismiss** — the popover is only `unparent()`d on the `row-activated` path. ESC key and click-outside fire the `closed` signal but no handler is connected.
2. **Label-text dispatch is fragile to i18n** — `_on_prompt_menu_row_activated` reads the child label text and compares to literal strings ("Copy path" / "Copy prompt"). Breaks silently under any translation.

## Scope

ONE phase, ONE file (`ui/views/left_panel.py`), plus test updates.

## Files to change

1. `ui/views/left_panel.py` — fix both bugs (see Edit 1, Edit 2 below)
2. `tests/test_left_panel.py` — update tests to match the new action-key dispatch (see Edit 3, Edit 4)

## Rules

- Use the **steelFramedCodeWriter** prompt at `prompts/steelFramedCodeWriter.md`
- READ ALL FILES BEFORE STARTING — `ui/views/left_panel.py` (especially lines 860-935), `tests/test_left_panel.py` (especially `TestPromptRowRightClick`), and `docs/post-mortems/2026-06-21-PROMPTS-TAB-RIGHT-CLICK-COPY-MENU-POST-MORTEM.md` §6.
- Architecture: handlers are GTK-free; ALL UI work lives in `ui/views/left_panel.py`. `prompts_handler.py` is unchanged.
- No new dependencies.

---

## Edit 1 — Popover leak fix

**Location:** `ui/views/left_panel.py`, in `_on_prompt_row_right_click` (currently around lines 875-914).

After the `popover.popup()` call at the end of `_on_prompt_row_right_click`, add:

```python
        popover.connect("closed", lambda *_: popover.unparent())
```

This ensures that **any** dismissal path (row-activated, ESC, click-outside, focus loss) results in the popover being unparented. The `row-activated` handler in `_on_prompt_menu_row_activated` already calls `popover.unparent()` after `popover.popdown()`. Both paths now leave the popover properly cleaned up.

**Important sequencing note:** When `popover.popdown()` is called in `_on_prompt_menu_row_activated`, the `closed` signal fires. Our new handler will then call `popover.unparent()` again. That's safe — calling `unparent()` on an already-unparented widget is a no-op in GTK4. No double-free.

**Verification command after edit:**
```bash
grep -n 'popover.connect("closed"' ui/views/left_panel.py
```

---

## Edit 2 — i18n-safe action-key dispatch

**Location:** `ui/views/left_panel.py`, two parts:

**Part A — In `_on_prompt_row_right_click`**, where the two menu rows are built (currently around lines 887-908), add an `_action` attribute to each row before appending:

For the "Copy path" row (currently lines 887-895):
```python
        # Row 1: Copy path
        copy_path_row = Gtk.ListBoxRow()
        copy_path_row.set_activatable(True)
        copy_path_row.set_selectable(False)
        copy_path_row._action = "copy_path"   # ← ADD THIS LINE
        copy_path_label = Gtk.Label(label="Copy path", xalign=0)
        # ... rest unchanged
```

For the "Copy prompt" row (currently lines 897-905):
```python
        # Row 2: Copy prompt
        copy_content_row = Gtk.ListBoxRow()
        copy_content_row.set_activatable(True)
        copy_content_row.set_selectable(False)
        copy_content_row._action = "copy_prompt"   # ← ADD THIS LINE
        copy_content_label = Gtk.Label(label="Copy prompt", xalign=0)
        # ... rest unchanged
```

**Part B — In `_on_prompt_menu_row_activated`** (currently around lines 916-925), replace the label-text dispatch with action-key dispatch:

**Before:**
```python
    def _on_prompt_menu_row_activated(self, _lb, menu_row, popover, source_row) -> None:
        """
        One of "Copy path" / "Copy prompt" was clicked. Dispatch and dismiss the popover.

        The source_row is the original prompt row (carries _filepath and _prompt_content).
        We identify the action by reading the child label text.
        """
        label_widget = menu_row.get_child()
        action = label_widget.get_text() if label_widget is not None else ""
        popover.popdown()
        popover.unparent()
        if action == "Copy path":
            self._on_copy_prompt_path(source_row)
        elif action == "Copy prompt":
            self._on_copy_prompt_content(source_row)
```

**After:**
```python
    def _on_prompt_menu_row_activated(self, _lb, menu_row, popover, source_row) -> None:
        """
        One of "Copy path" / "Copy prompt" was clicked. Dispatch and dismiss the popover.

        The source_row is the original prompt row (carries _filepath and _prompt_content).
        We identify the action by reading the row's _action attribute (set at row build time),
        NOT by parsing the label text. This is robust to i18n: the displayed label can be
        translated without breaking dispatch.
        """
        popover.popdown()
        # Popover.unparent() also fires via the "closed" signal handler connected in
        # _on_prompt_row_right_click. The popdown() call schedules the close, so the
        # unparent happens shortly after. No need to call it explicitly here.
        action = getattr(menu_row, "_action", None)
        if action == "copy_path":
            self._on_copy_prompt_path(source_row)
        elif action == "copy_prompt":
            self._on_copy_prompt_content(source_row)
        # Unknown action → no-op. Defensive: if a future menu row is added without
        # _action, the click is silently ignored rather than crashing.
```

**Verification commands after edit:**
```bash
# Confirm action attrs are set on rows
grep -n '_action = ' ui/views/left_panel.py

# Confirm label-text dispatch is gone
grep -n 'label_widget\|get_text()' ui/views/left_panel.py | grep -i prompt

# Should print: nothing (the label_widget line is gone)
```

---

## Edit 3 — Update test_right_click_creates_popover

**Location:** `tests/test_left_panel.py`, the existing `test_right_click_creates_popover` test (or whichever test name currently exercises `_on_prompt_menu_row_activated` via the captured label text).

If any test reads the popover's child label text to identify the action, **change it** to read `row._action` instead. The action keys are now `"copy_path"` and `"copy_prompt"` (not `"Copy path"` / `"Copy prompt"`).

**Concrete search:**
```bash
grep -n '"Copy path"\|"Copy prompt"' tests/test_left_panel.py
```

If any test asserts on those literal strings, replace `"Copy path"` → `"copy_path"` and `"Copy prompt"` → `"copy_prompt"` (these are now action keys, not labels).

If a test reads `label_widget.get_text()` to dispatch, rewrite it to read `menu_row._action` directly.

**Add a new test** for the i18n robustness:

```python
    def test_menu_row_dispatch_uses_action_key_not_label(self, controller, ...):
        """
        Regression: label-text dispatch (Bug #2 in Tier-3 follow-ups) broke under any
        translation. Fix: each menu row carries an _action attribute set at row build
        time, and dispatch reads _action — not the child label text.
        """
        # Build a fake source row with the required attrs
        source_row = type("FakeRow", (), {"_filepath": "/tmp/x.md", "_prompt_content": "x"})()
        # Build the two menu rows the same way the production code does
        from ui.views.left_panel import LeftPanel  # or however the test imports
        # ... simulate _on_prompt_row_right_click to construct the popover and menu rows
        # ... confirm each menu row has _action set
        # ... call _on_prompt_menu_row_activated with each menu row
        # ... confirm the right helper was called (mock _on_copy_prompt_path / _on_copy_prompt_content)
        # ... THEN: change the child label to a non-English string and confirm dispatch still works
```

The exact test structure depends on the existing test patterns in `tests/test_left_panel.py`. Read the existing 8 tests and match the style. **At minimum**, the new test must:
1. Verify `menu_row._action == "copy_path"` for the first menu row
2. Verify `menu_row._action == "copy_prompt"` for the second menu row
3. Verify that mutating the child label text (e.g., to French "Copier le chemin") does NOT break dispatch

---

## Edit 4 — Add a test for the popover leak fix

**Location:** `tests/test_left_panel.py`.

**Add a new test** that confirms the `closed` signal handler is connected:

```python
    def test_popover_closed_handler_unparents(self, controller, ...):
        """
        Regression: popover leak on ESC / click-outside dismiss (Bug #1 in Tier-3
        follow-ups). Fix: _on_prompt_row_right_click connects the "closed" signal
        to a handler that calls popover.unparent().
        """
        # ... simulate _on_prompt_row_right_click to construct the popover
        # ... confirm the popover has a "closed" signal handler connected
        # ... fire the "closed" signal (or call popdown() to trigger it)
        # ... confirm popover.get_parent() is None after the signal fires
```

**Verification:** the test should FAIL if the `popover.connect("closed", ...)` line in Edit 1 is removed.

Run this verification yourself: comment out Edit 1's line, re-run the test, confirm it FAILS, restore the line, confirm it PASSES. Paste the output in your COMPLETENESS report.

---

## Verification commands (run all of these and paste output in your report)

```bash
# 1. Confirm the closed handler is connected
grep -n 'popover.connect("closed"' ui/views/left_panel.py

# 2. Confirm action attrs are set on menu rows
grep -n '_action = ' ui/views/left_panel.py

# 3. Confirm label-text dispatch is gone (should print 0 lines for the label dispatch line)
grep -n 'label_widget\|get_text()' ui/views/left_panel.py | grep -v "^.*#" || echo "no label dispatch found"

# 4. Confirm no test asserts on literal "Copy path" / "Copy prompt" as the dispatch key
grep -n '"Copy path"\|"Copy prompt"' tests/test_left_panel.py || echo "no literal-string dispatch in tests"

# 5. Run new and existing tests for this feature
pytest tests/test_left_panel.py -v

# 6. Run regression-proof verification (Edit 4): comment out closed handler, re-run new test
#    should FAIL, then restore
# (paste output of FAIL before restoring)

# 7. Run the full selective test suite (8 test files that touch left_panel.py)
pytest tests/test_left_panel.py tests/test_prompts_handler.py tests/test_feed_handler.py \
       tests/test_chat_handler.py tests/test_agent_list_handler.py tests/test_architecture.py \
       tests/test_window.py tests/test_handlers_init.py -q
```

---

## Report back

When done, paste:

1. `git diff --stat` showing only `ui/views/left_panel.py` and `tests/test_left_panel.py` changed
2. Output of all 7 verification commands above
3. A COMPLETENESS block (mandatory — see steelFramedCodeWriter.md Step 6.5):

```
COMPLETENESS:
- [x] Edit 1: popover.connect("closed", ...) added — evidence: <grep output>
- [x] Edit 2: _action attrs set on menu rows; dispatch reads _action — evidence: <grep output>
- [x] Edit 3: existing test updated; new test for i18n robustness added — evidence: <test name + count>
- [x] Edit 4: new test for popover leak; regression-proof verified — evidence: <FAIL→PASS paste>
- [x] Architecture boundary preserved — evidence: <grep of prompts_handler.py for GTK imports, should be 0>
```

**Word marker:** please proceed with the fix per these instructions.

## Out of scope

- The 4 spec files in `docs/specs/` left from the previous right-click feature phase are working artifacts and remain untracked. Do not commit them.
- The pre-existing 253-fence ARCHITECTURE.md bug is already fixed in commit `2a05240`. Do not touch ARCHITECTURE.md.
- No new files. No refactoring of the larger left_panel.py structure.