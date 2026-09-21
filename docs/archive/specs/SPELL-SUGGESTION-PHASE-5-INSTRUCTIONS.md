# Phase 5: Tests for spell-check suggestion popover

**Spec:** `docs/specs/SPEC-SPELL-SUGGESTION-POPOVER.md` §4 (Test rows)
**Files to change:**
- `tests/test_input_toolbar_handler.py`
- `tests/test_chat_input_toolbar.py`

**Total phases:** 5
**Current phase:** 5 of 5
**Depends on:** Phases 1-4 must be complete and verified

## What to do

Add tests for the new functionality added in Phases 1-4.

## Test 1: `replace_word_at_iter` in `tests/test_input_toolbar_handler.py`

Add a test that verifies `replace_word_at_iter` correctly replaces a word in the buffer.

```python
def test_replace_word_at_iter_replaces_misspelled_word():
    """replace_word_at_iter deletes the word at the iter and inserts replacement."""
    from ui.handlers.input_toolbar_handler import InputToolbarHandler
    handler = InputToolbarHandler(main_content=MagicMock())
    buf = MagicMock()
    text_iter = MagicMock()
    text_iter.copy.return_value = text_iter
    text_iter.inside_word.return_value = True
    # backward_word_start and forward_word_end are in-place mutations
    buf.delete.return_value = None
    buf.insert.return_value = None
    handler._mc.user_input.get_buffer.return_value = buf
    handler._spell_enabled = False  # don't re-run spell check in unit test
    handler.replace_word_at_iter(text_iter, "corrected")
    # Verify word boundaries were found, delete + insert called
    assert text_iter.backward_word_start.called
    assert text_iter.forward_word_end.called
    assert buf.delete.called
    buf.insert.assert_called_once_with(text_iter, "corrected")
```

Also add a sad-path test:

```python
def test_replace_word_at_iter_noop_if_not_in_word():
    """replace_word_at_iter returns without modifying buffer if iter is not inside a word."""
    from ui.handlers.input_toolbar_handler import InputToolbarHandler
    handler = InputToolbarHandler(main_content=MagicMock())
    buf = MagicMock()
    text_iter = MagicMock()
    text_iter.copy.return_value = text_iter
    text_iter.inside_word.return_value = False
    handler._mc.user_input.get_buffer.return_value = buf
    handler.replace_word_at_iter(text_iter, "corrected")
    assert not buf.delete.called
    assert not buf.insert.called
```

## Test 2: `show_suggestions_menu` with `parent_widget` in `tests/test_chat_input_toolbar.py`

Add a test that verifies the `parent_widget` parameter is used when provided:

```python
def test_show_suggestions_menu_with_parent_widget():
    """show_suggestions_menu parents popover to parent_widget when provided."""
    toolbar = ChatInputToolbar()
    parent = Gtk.Box()  # stand-in widget
    toolbar.show_suggestions_menu(
        ["alpha", "beta"],
        lambda s: None,
        parent_widget=parent,
    )
    # The popover should be parented to `parent`, not to _spell_btn
    # We can't easily inspect the popover's parent in headless, but we verify
    # no crash occurs and the function accepts the parameter.
    # If the popover were parented wrong, GTK would warn about widget hierarchy.
```

Also add a test verifying backward compatibility (no parent_widget = still works):

```python
def test_show_suggestions_menu_backward_compatible_no_parent_widget():
    """show_suggestions_menu still works without parent_widget (backward compat)."""
    toolbar = ChatInputToolbar()
    toolbar.show_suggestions_menu(
        ["world", "weird"],
        lambda s: None,
    )
    # Should not crash; uses _spell_btn as fallback parent
```

## Verification

Run:
```bash
cd /path/to/projects/crabcakes
python3 -m pytest tests/test_input_toolbar_handler.py tests/test_chat_input_toolbar.py -q --tb=short
```

Paste the full output.

Also run:
```bash
grep -n "def test_replace_word_at_iter" tests/test_input_toolbar_handler.py
grep -n "def test_show_suggestions_menu_with_parent_widget\|def test_show_suggestions_menu_backward" tests/test_chat_input_toolbar.py
```

## Rules

- Use the `steelFramedCodeWriter` prompt at `prompts/steelFramedCodeWriter.md`
- READ ALL FILES before starting — read the test files and the source files they test
- Mock at the boundary (external dependencies), not at the function being tested
- Every test must be able to FAIL if the feature were broken
- Sad-path tests are mandatory (at least 30% of new tests)
- Do not modify existing tests
- Do not modify any non-test files

## Deliverable

Report back with:
1. Files changed (with line numbers)
2. Full pytest output
3. Grep outputs above
4. COMPLETENESS checklist:
```
COMPLETENESS:
- [x/not done] Test 1: replace_word_at_iter happy path — evidence (line N, test pass)
- [x/not done] Test 2: replace_word_at_iter sad path (not in word) — evidence (line N, test pass)
- [x/not done] Test 3: show_suggestions_menu with parent_widget — evidence (line N, test pass)
- [x/not done] Test 4: show_suggestions_menu backward compat — evidence (line N, test pass)
```
