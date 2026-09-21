# Phase 2 Instructions — Test File Audit + Add Missing Test

**Phase 1 status:** ACCEPTED (6 code edits verified). The test file was created in Phase 1 as a scope violation, but it does not block acceptance.

**Spec:** `docs/specs/PROMPTS-TAB-RIGHT-CLICK-COPY-MENU-PHASE-1-SPEC.md` (read in full)
**Audit report:** See the Phase-1 audit section below — 7 bugs found, 1 HIGH-severity finding blocks Phase 2 completion.

## CONTEXT

QTR created `tests/test_left_panel.py` in Phase 1 with 7 tests. Adversarial audit found:

- **BUG #4 (HIGH):** `test_right_click_handler_ignores_multipress` calls the handler directly. It would PASS even if the `Gtk.GestureClick` were never attached to the row. This violates Rule 4 of steelFramedCodeWriter and adversarialDebugger §11 ("test the user-facing behavior, not the helper").
- **BUG #5 (MEDIUM):** `test_copy_status_label_shows_and_clears` schedules a 2500ms timeout but never invokes the closure to verify it actually clears the label text.
- **BUG #8 (MEDIUM):** Popover leak when popover is dismissed by ESC/click-outside (not via menu selection).

## TASK

### Edit 1: Add Test 8 — verify the gesture is actually attached to the row

**File:** `tests/test_left_panel.py`

**Why:** This test makes the user-facing wiring fail-loud. If a future refactor removes the `add_controller` call in `_build_prompt_row`, this test will fail. The existing tests pass even if Edit 5 of Phase 1 is reverted.

**Test (append to `TestPromptRowRightClick`):**

```python
    def test_prompt_row_has_right_click_gesture_attached(self):
        """Verify _build_prompt_row actually attaches a right-click GestureClick controller
        to the row. This tests the USER-FACING wiring (Edit 5), not just the helper.

        Per steelFramedCodeWriter Rule 4 + adversarialDebugger §11: a test that only calls
        the helper would pass even if the gesture were never attached, hiding a real
        regression in the right-click wiring.
        """
        panel = LeftPanel()
        panel._prompts_handler = MagicMock()

        prompt = {
            'filepath': '/abs/path/to/prompt.md',
            'name': 'test-prompt',
            'content': 'content',
            'is_favorite': False,
            'lines': 1,
            'size': 1,
            'last_used_str': ''
        }

        row = panel._build_prompt_row(prompt)

        # Inspect the row's controllers — at least one must be a GestureClick
        # configured for the secondary (right) mouse button.
        from gi.repository import Gdk
        observers = row.observe_controllers()
        found_right_click = False
        for ctrl in observers:
            # Gtk.EventController is the base class; Gtk.GestureClick is a subclass.
            if isinstance(ctrl, Gtk.GestureClick):
                if ctrl.get_button() == Gdk.BUTTON_SECONDARY:
                    found_right_click = True
                    break

        assert found_right_click, (
            "Prompt row must have a Gtk.GestureClick controller attached with "
            "button=Gdk.BUTTON_SECONDARY (right-click). If this test fails, the "
            "right-click gesture wiring was removed from _build_prompt_row."
        )
```

### Edit 2: Strengthen `test_copy_status_label_shows_and_clears`

**File:** `tests/test_left_panel.py`, function `test_copy_status_label_shows_and_clears`

**Current weakness:** patches `GLib.timeout_add` but never invokes the closure.

**Fix:** invoke the closure returned by the mock to verify it actually clears the label.

**Replace the existing function body with:**

```python
    def test_copy_status_label_shows_and_clears(self):
        """Verify _show_prompt_copy_status sets label text AND the scheduled
        timeout callback actually clears the label."""
        panel = LeftPanel()
        panel._prompt_copy_status_label = Gtk.Label()
        panel._prompt_copy_status_timeout_id = None

        captured_callbacks = []
        def fake_timeout_add(ms, cb):
            captured_callbacks.append((ms, cb))
            return 12345

        with patch('gi.repository.GLib.timeout_add', side_effect=fake_timeout_add):
            panel._show_prompt_copy_status("Copied path")

            # 1. Label text is set
            assert panel._prompt_copy_status_label.get_text() == "Copied path"
            # 2. Timeout was scheduled with 2500ms
            assert len(captured_callbacks) == 1
            assert captured_callbacks[0][0] == 2500
            # 3. Invoke the scheduled callback and verify it clears the label
            captured_callbacks[0][1]()
            assert panel._prompt_copy_status_label.get_text() == ""
```

## VERIFICATION (run all yourself, paste output)

```bash
# 1. All tests in test_left_panel.py pass
cd /path/to/projects/crabcakes && pytest tests/test_left_panel.py -v --tb=short

# 2. Test 8 actually fails if Edit 5 (gesture wiring) is reverted — REGRESSION-PROOF CHECK
# Temporarily comment out the add_controller line in _build_prompt_row, run Test 8, expect FAIL.
# Then restore. (Do NOT commit the revert.)

# 3. Full test suite still passes (no regressions in other test files)
cd /path/to/projects/crabcakes && pytest tests/test_left_panel.py tests/test_prompts_handler.py tests/test_feed_handler.py -q --tb=short

# 4. Total test count
cd /path/to/projects/crabcakes && pytest tests/test_left_panel.py --collect-only -q 2>&1 | tail -3
```

## REGRESSION-PROOF CHECK (Edit 1 / Test 8)

This is the critical adversarial verification per steelFramedCodeWriter Rule 4 + adversarialDebugger §11. Run this manually:

1. Open `ui/views/left_panel.py` in a text editor.
2. Find the block at lines 755-758 (the right-click `add_controller` call).
3. Comment out the `right_ctrl` block (3 lines).
4. Run `pytest tests/test_left_panel.py::TestPromptRowRightClick::test_prompt_row_has_right_click_gesture_attached -v --tb=short`
5. **Expected:** FAIL with `AssertionError: Prompt row must have a Gtk.GestureClick controller attached with button=Gdk.BUTTON_SECONDARY`
6. Restore the commented-out lines.
7. Re-run — expect PASS.

If step 5 does NOT fail, the test is broken (regression-proof fails). Fix the test, do not declare done.

## REPORT BACK

Reply with:
1. **Verification outputs** (paste actual output of all 4 commands)
2. **Regression-proof check log** (paste step 5's FAIL output and step 7's PASS output)
3. **COMPLETENESS checklist** with the literal `COMPLETENESS:` marker

Use the word marker "please write" in your reply.

— Qaster
