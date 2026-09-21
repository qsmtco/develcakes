# Phase 3 Instructions — Documentation Update (ARCHITECTURE.md)

**Phase 2 status:** ACCEPTED. Test file now has 8 tests, including regression-proof Test 8 (gesture wiring) and strengthened Test 6 (closure invocation).

**Spec:** `docs/specs/PROMPTS-TAB-RIGHT-CLICK-COPY-MENU-PHASE-1-SPEC.md`
**Audit context:** Phase 1 introduced the right-click copy menu; Phase 2 added regression-proof tests. Phase 3 makes the docs reflect both.

## READ THESE FILES IN FULL BEFORE WRITING

> Per steelFramedCodeWriter Rule 1.

1. `docs/ARCHITECTURE.md` (sections 3.7, 3.13, file inventory near line 3417, test inventory near line 3040)
2. `ui/views/left_panel.py` (lines 622-640 for the new status label, lines 680-760 for the row + gesture, lines 860-972 for the 6 new methods)
3. `tests/test_left_panel.py` (the 8 tests, especially the new Test 8)

## TASK

### Edit 1: Update §3.7 `ui/views/left_panel.py` — Prompts tab section

**File:** `docs/ARCHITECTURE.md`, around line 422-440

**Current text in the Prompts tab paragraph (find it, replace ONLY the Prompts tab paragraph, not the Agents or Projects paragraphs):**

```
**Prompts tab:** PromptsHandler-backed list with search, favorites, and rich metadata rows. Star/favorite persisted to `~/.config/crabcakes/favorites.json`. Double-click or `+` button calls `on_prompt_loaded(filepath, name, content)`, which loads content into chat input. Search filters by name (case-insensitive). Favorites sort to top.
```

**Replace with:**

```
**Prompts tab:** PromptsHandler-backed list with search, favorites, and rich metadata rows. Star/favorite persisted to `~/.config/crabcakes/favorites.json`. Double-click or `+` button calls `on_prompt_loaded(filepath, name, content)`, which loads content into chat input. Search filters by name (case-insensitive). Favorites sort to top. Right-click on a prompt row opens a 2-item popover menu ("Copy path" / "Copy prompt"); the selection is copied to the system clipboard via `Gdk.Display.get_clipboard()` and a transient "Copied path" / "Copied prompt" confirmation appears in the tab header for 2.5s (auto-cleared via `GLib.timeout_add`).
```

### Edit 2: Add a new sub-section §3.7a documenting the right-click menu mechanism

**File:** `docs/ARCHITECTURE.md`, immediately AFTER the §3.7 "Public API" block (find `panel.set_toggle_agent_callback(cb)`) and BEFORE `### 3.8` `ui/views/file_tree.py` — FileTree Widget`.

**Add (as a new `### 3.7a` section):**

```markdown
### 3.7a Prompts Tab Right-Click Copy Menu

**Responsibility:** View-layer (LeftPanel) feature. Right-click on a prompt row → 2-item popover (Copy path / Copy prompt) → clipboard write → transient status feedback.

**Architecture boundary (per §3.13):** All GTK/widget code lives in `left_panel.py` (view owner). `PromptsHandler` (data owner) is unchanged. No GTK imports in `prompts_handler.py`. The view consumes `prompt['filepath']` and `prompt['content']` from the handler's scan output and stashes them as row attributes (`_filepath`, `_prompt_content`) at build time.

**Wiring:**
- `LeftPanel._build_prompt_row()` attaches a `Gtk.GestureClick` controller with `button=Gdk.BUTTON_SECONDARY` to every prompt row. The `pressed` signal connects to `_on_prompt_row_right_click`.
- `_on_prompt_row_right_click(ctrl, n_press, x, y, row)` filters out multi-press (`n_press != 1`) and rows without `_filepath`, then constructs a `Gtk.Popover` with a 2-row `Gtk.ListBox`. Popover parent is the source row.
- `_on_prompt_menu_row_activated(_lb, menu_row, popover, source_row)` reads the child label text to dispatch "Copy path" or "Copy prompt", then `popdown()` + `unparent()`s the popover.
- `_on_copy_prompt_path(row)` and `_on_copy_prompt_content(row)` read `row._filepath` / `row._prompt_content` and call the local `_copy_text_to_clipboard()` helper.
- `_copy_text_to_clipboard(text)` uses `Gdk.Display.get_default().get_clipboard().set(text)` — no-op when display is unavailable (headless test env).
- `_show_prompt_copy_status(message)` writes the message into the status label appended to the Prompts tab header, then schedules a 2.5s `GLib.timeout_add` to clear it. Pending timeout is cancelled before a new one is scheduled.

**Status label location:** The transient status label (`_prompt_copy_status_label`) is appended to the Prompts tab header `[title, search, status_label]` and right-aligned via `set_xalign(1.0)`. Styled with `.dim-label` CSS class.

**Test coverage:** `tests/test_left_panel.py` — 8 tests in `TestPromptRowRightClick`:
- `test_prompt_row_has_filepath_and_content_attrs` — row attributes set from prompt dict
- `test_copy_path_calls_clipboard_with_filepath` — clipboard called with filepath
- `test_copy_prompt_calls_clipboard_with_content` — clipboard called with content
- `test_copy_path_skips_when_filepath_missing` — defensive skip
- `test_copy_prompt_skips_when_content_missing` — defensive skip
- `test_copy_status_label_shows_and_clears` — label set + closure clears it
- `test_right_click_handler_ignores_multipress` — n_press != 1 skipped
- `test_prompt_row_has_right_click_gesture_attached` — **regression-proof**: FAILS if `add_controller` is removed from `_build_prompt_row`

**Known follow-ups (not blocking):**
- Popover leak on ESC / click-outside dismissal (the `row-activated` path always `unparent()`s, but other dismiss paths don't). Fix: wire `popover.connect("closed", lambda *_: popover.unparent())`.
- Label-text dispatch ("Copy path" / "Copy prompt") would silently no-op on localized strings. Future: store an action key on each row instead of parsing label text.
```

### Edit 3: Update file inventory near line 3417

**File:** `docs/ARCHITECTURE.md`, find the line:
```
│       ├── left_panel.py         # ~838 lines — LeftPanel (Prompts/Agents/Projects notebook)
```

**Replace with:**
```
│       ├── left_panel.py         # ~974 lines — LeftPanel (Prompts/Agents/Projects notebook + right-click copy menu)
```

### Edit 4: Add `test_left_panel.py` to the test inventory near line 3040

**File:** `docs/ARCHITECTURE.md`, in the "Test coverage" bulleted list near line 3040-3055. Add a new bullet after the `test_prompts_handler.py` line:

```
- `tests/test_left_panel.py` — LeftPanel: right-click copy menu (gesture wiring, clipboard, status label), 8 tests
```

### Edit 5: Add `test_left_panel.py` to the test file tree near line 3537

**File:** `docs/ARCHITECTURE.md`, in the test files tree section. Find the line with `test_prompts_handler.py` and add a new line:

```
    ├── test_left_panel.py
```

Place it alphabetically (before `test_prompts_handler.py`).

## VERIFICATION (run all yourself, paste output)

```bash
# 1. §3.7 Prompts tab paragraph updated
cd /path/to/projects/crabcakes && grep -n "Right-click on a prompt row opens a 2-item popover" docs/ARCHITECTURE.md

# 2. §3.7a added
cd /path/to/projects/crabcakes && grep -n "### 3.7a Prompts Tab Right-Click Copy Menu" docs/ARCHITECTURE.md

# 3. file inventory updated
cd /path/to/projects/crabcakes && grep -n "left_panel.py.*974" docs/ARCHITECTURE.md

# 4. test inventory added
cd /path/to/projects/crabcakes && grep -n "test_left_panel.py.*LeftPanel" docs/ARCHITECTURE.md

# 5. test file tree updated
cd /path/to/projects/crabcakes && grep -n "├── test_left_panel.py" docs/ARCHITECTURE.md

# 6. No accidental damage to the rest of the file
cd /path/to/projects/crabcakes && git diff --stat docs/ARCHITECTURE.md

# 7. Markdown is valid (no broken backticks, no unmatched code fences)
cd /path/to/projects/crabcakes && python3 -c "
import re
with open('docs/ARCHITECTURE.md') as f:
    text = f.read()
fences = re.findall(r'^\`\`\`', text, re.MULTILINE)
print(f'Code fences: {len(fences)} (should be even)')
"
```

## REPORT BACK

Reply with:
1. **Files changed** (list each path)
2. **Verification outputs** (paste all 7 command outputs)
3. **Diff summary** (paste the actual `git diff --stat` output)
4. **COMPLETENESS** checklist (literal `COMPLETENESS:` marker)
5. **Cross-checks** (per steelFramedCodeWriter Step 6.7): "I read the existing §3.7, §3.13, file inventory, and test inventory sections. I did not modify any other sections."

Use the word marker "please write" in your reply.

— Qaster
