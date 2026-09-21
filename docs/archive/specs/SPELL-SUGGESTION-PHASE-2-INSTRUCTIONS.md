# Phase 2: Fix `show_suggestions_menu` parent widget in ChatInputToolbar

**Spec:** `docs/specs/SPEC-SPELL-SUGGESTION-POPOVER.md` §2.3
**File to change:** `ui/views/chat_input_toolbar.py`
**Total phases:** 5
**Current phase:** 2 of 5

## What to do

Modify `show_suggestions_menu` to accept an optional `parent_widget` parameter and add a `closed` signal handler to unparent the popover on dismiss (prevents widget leak).

## Exact changes

### Change 1: Signature (line 243)

**Current:**
```python
def show_suggestions_menu(self, suggestions: list[str], callback: callable):
```

**New:**
```python
def show_suggestions_menu(self, suggestions: list[str], callback: callable, parent_widget=None):
```

### Change 2: Parent widget logic (line 250)

**Current:**
```python
popover.set_parent(self._spell_btn)
```

**New:**
```python
if parent_widget is not None:
    popover.set_parent(parent_widget)
else:
    popover.set_parent(self._spell_btn)
```

### Change 3: Add `closed` signal handler

After the line `popover.set_child(vbox)` (currently around line 270) and BEFORE the `if self.get_root() is not None:` popup guard, add:

```python
popover.connect("closed", lambda *_: popover.unparent())
```

This prevents popover widget leak on ESC/click-outside dismiss — same bug class fixed in the Tier-3 left_panel popover-leak fix.

## Verification

After making changes, run:

```bash
cd /path/to/projects/crabcakes
python3 -m pytest tests/test_chat_input_toolbar.py -q --tb=short
```

Paste the full output.

Also run:
```bash
grep -n "def show_suggestions_menu" ui/views/chat_input_toolbar.py
grep -n "parent_widget" ui/views/chat_input_toolbar.py
grep -n "popover.connect.*closed" ui/views/chat_input_toolbar.py
```

## Backward compatibility

Existing tests call `show_suggestions_menu(["world", ...], callback)` without `parent_widget`. With `parent_widget=None` default, these tests must continue to pass UNCHANGED. If any existing test breaks, that is a regression.

## Rules

- Use the `steelFramedCodeWriter` prompt at `prompts/steelFramedCodeWriter.md`
- READ ALL FILES before starting — read `ui/views/chat_input_toolbar.py` in full first
- Do not modify any other file
- Do not reformat adjacent code
- Do not change the docstring's meaning (just add note about parent_widget if needed)

## Deliverable

Report back with:
1. Files changed (with line numbers)
2. Full pytest output
3. Grep outputs above
4. COMPLETENESS checklist:
```
COMPLETENESS:
- [x/not done] Edit 1: Added parent_widget parameter — evidence (line N)
- [x/not done] Edit 2: Parent widget conditional logic — evidence (line N)
- [x/not done] Edit 3: Added closed signal handler for unparent — evidence (line N)
```
