# SPEC: Prompts Tab — Right-Click Copy Menu

**Date:** 2026-06-21
**Author:** Qaster (spec author, independent of implementation)
**Status:** Draft — for implementation
**Implements:** ad-hoc feature request from Captain; no proposal doc
**Depends on:** none
**Target branch:** main

> Architecture compliance: prompts data is owned by `ui/handlers/prompts_handler.py`; view/widget tree is owned by `ui/views/left_panel.py` (ARCHITECTURE.md §3.13). The view layer may call existing public methods on the handler; the handler must not gain GTK imports.

---

## 1. Overview

### Problem statement
Users browsing the Prompts tab in the left-hand pane currently have no way to capture a prompt's file path or its raw contents without leaving the app. Common workflow — "I want to reference this prompt in a bug report" or "let me check the file" — requires using the file manager or a terminal.

### Solution summary
Add a right-click context menu to each prompt card in the Prompts tab. The menu has two items:

- **Copy path** — copies the absolute path of the prompt's `.md` file to the system clipboard.
- **Copy prompt** — copies the raw file contents (as read from disk) to the system clipboard.

A small confirmation (transient status label in the Prompts tab header) appears after a successful copy.

### Scope

| In scope | Out of scope |
|---|---|
| Right-click on prompt `Gtk.ListBoxRow` in the Prompts tab | Right-click on the `+ Add Prompt` card (already wired to its own popover) |
| Two menu items: "Copy path", "Copy prompt" | Editing or deleting the prompt from the menu |
| Confirmation feedback (transient label) | Toast overlay (none exists in the codebase; spec introduces a minimal in-tab label) |
| Reading file contents from disk via `PromptsHandler.get_prompt_content` | Caching file contents in the view layer |
| Pango-escaped menu labels (MED-9) per existing `session_menu.py` pattern | Keyboard accelerators (out of scope for Phase 1) |

### Architecture principles that apply
- **§3.13** — PromptsHandler is the data owner; left_panel.py is the view. No GTK imports in the handler.
- **MED-9** — All interpolated values in Pango markup must be escaped via `GLib.markup_escape_text()`.
- **§4.2 (popover menu pattern)** — `session_menu.py` is the established pattern for right-click popovers. This spec copies that pattern.
- **Rule 4 (single source of truth)** — `prompts_handler.get_prompt_content()` is reused for the file read; do not duplicate the file-read logic in the view.

---

## 2. Changes by File

### 2.1 `ui/views/left_panel.py` — add right-click gesture + menu to each prompt row

**What changes:**
- Add a `Gtk.GestureClick` configured for `Gdk.BUTTON_SECONDARY` to each prompt `Gtk.ListBoxRow` in `_build_prompt_row`. The `+` row does **not** get this gesture (it is a separate widget and has no `_filepath`).
- Add a new private method `_show_prompt_context_menu(row, n_press, x, y)` that builds a `Gtk.Popover` with two `Gtk.ListBoxRow`s ("Copy path", "Copy prompt") and pops it up at the row.
- Add a new private method `_on_copy_prompt_path(row)` that copies `row._filepath` to the clipboard and fires the transient confirmation.
- Add a new private method `_on_copy_prompt_content(row)` that copies `row._prompt_content` to the clipboard and fires the transient confirmation.
- Add a transient confirmation label to the Prompts tab header, positioned to the right of the search entry. Default text is empty (`""`); after a copy, the label is set to e.g. `"Copied path"` for ~2.5 seconds, then cleared via `GLib.timeout_add`.
- Add the `import gi; gi.require_version('Gdk', '4.0')` and `from gi.repository import Gdk` lines at the top of the file (currently only Gtk, Gio, GLib are imported — verified at left_panel.py:11-13).

**Exact method signatures (verified against existing code):**

```python
# Added to LeftPanel.__init__ state block (~line 47-65 area)
self._prompt_copy_status_label = None   # built in _build_prompts_tab
self._prompt_copy_status_timeout_id = None
```

```python
# In _build_prompts_tab (after search_entry setup, ~line 624)
# Add a status label to the header, right of the search entry.
status_label = Gtk.Label(label="")
status_label.set_valign(Gtk.Align.CENTER)
status_label.set_margin_start(4)
status_label.set_margin_end(4)
status_label.add_css_class("dim-label")
status_label.set_xalign(1.0)  # right-align text within label
self._prompt_copy_status_label = status_label
header.append(status_label)  # header is the [title, search, status] hbox
```

```python
# Inside _build_prompt_row, after row_box is built (~line 709)
# Attach right-click gesture that opens the context menu.
right_ctrl = Gtk.GestureClick()
right_ctrl.set_button(Gdk.BUTTON_SECONDARY)
right_ctrl.connect("pressed", self._on_prompt_row_right_click, row)
row.add_controller(right_ctrl)
```

```python
def _on_prompt_row_right_click(self, ctrl, n_press, x, y, row) -> None:
    """
    Right-click on a prompt row — show the copy menu.

    Args:
        ctrl:    Gtk.GestureClick (sender, not used).
        n_press: int — number of presses (only respond to single click).
        x, y:    float — local click coordinates (unused; popover anchors to row).
        row:     Gtk.ListBoxRow — the right-clicked row (carries _filepath + _prompt_content).
    """
    if n_press != 1:
        return
    if not hasattr(row, "_filepath"):
        return  # defensive: skip non-prompt rows (e.g., the "+" row)

    popover = Gtk.Popover()
    popover.set_parent(row)

    vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    vbox.set_margin_top(6)
    vbox.set_margin_bottom(6)
    vbox.set_margin_start(6)
    vbox.set_margin_end(6)

    list_box = Gtk.ListBox()
    list_box.set_selection_mode(Gtk.SelectionMode.NONE)

    # Row 1: Copy path
    copy_path_row = Gtk.ListBoxRow()
    copy_path_row.set_activatable(True)
    copy_path_row.set_selectable(False)
    copy_path_label = Gtk.Label(label="Copy path", xalign=0)
    copy_path_label.set_margin_top(4)
    copy_path_label.set_margin_bottom(4)
    copy_path_label.set_margin_start(8)
    copy_path_label.set_margin_end(8)
    copy_path_row.set_child(copy_path_label)
    list_box.append(copy_path_row)

    # Row 2: Copy prompt
    copy_content_row = Gtk.ListBoxRow()
    copy_content_row.set_activatable(True)
    copy_content_row.set_selectable(False)
    copy_content_label = Gtk.Label(label="Copy prompt", xalign=0)
    copy_content_label.set_margin_top(4)
    copy_content_label.set_margin_bottom(4)
    copy_content_label.set_margin_start(8)
    copy_content_label.set_margin_end(8)
    copy_content_row.set_child(copy_content_label)
    list_box.append(copy_content_row)

    list_box.connect("row-activated", self._on_prompt_menu_row_activated, popover, row)
    vbox.append(list_box)
    popover.set_child(vbox)
    popover.popup()
```

```python
def _on_prompt_menu_row_activated(self, _lb, menu_row, popover, source_row) -> None:
    """
    One of "Copy path" / "Copy prompt" was clicked. Dispatch and dismiss the popover.

    The source_row is the original prompt row (carries _filepath and _prompt_content).
    We identify the action by reading the child label text (MED-9-safe: the labels are
    static literal strings "Copy path" / "Copy prompt", not user input).
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

```python
def _on_copy_prompt_path(self, row) -> None:
    """Copy the absolute path of the right-clicked prompt's file to the clipboard."""
    filepath = getattr(row, "_filepath", None)
    if not filepath:
        return
    self._copy_text_to_clipboard(filepath)
    self._show_prompt_copy_status("Copied path")

def _on_copy_prompt_content(self, row) -> None:
    """Copy the raw file contents of the right-clicked prompt to the clipboard."""
    content = getattr(row, "_prompt_content", None)
    if content is None:
        return
    self._copy_text_to_clipboard(content)
    self._show_prompt_copy_status("Copied prompt")
```

```python
def _copy_text_to_clipboard(self, text: str) -> None:
    """Copy text to the system clipboard using GTK4 clipboard API.

    Pattern mirrors chat_bubble._copy_to_clipboard (verified at chat_bubble.py:1004-1010).
    Returns silently if no display is available (e.g., headless test env).
    """
    display = Gdk.Display.get_default()
    if display is None:
        return
    clipboard = display.get_clipboard()
    clipboard.set(text)

def _show_prompt_copy_status(self, message: str) -> None:
    """Show a transient confirmation in the prompts tab header for ~2.5s."""
    if self._prompt_copy_status_label is None:
        return
    self._prompt_copy_status_label.set_text(message)
    # Cancel any pending clear, then schedule a new one.
    if self._prompt_copy_status_timeout_id is not None:
        try:
            GLib.source_remove(self._prompt_copy_status_timeout_id)
        except Exception:
            pass
        self._prompt_copy_status_timeout_id = None
    def _clear():
        if self._prompt_copy_status_label is not None:
            self._prompt_copy_status_label.set_text("")
        self._prompt_copy_status_timeout_id = None
        return GLib.SOURCE_REMOVE
    self._prompt_copy_status_timeout_id = GLib.timeout_add(2500, _clear)
```

**Imports required (added at top of file):**
```python
from gi.repository import Gdk  # Gdk is NOT currently imported — verified at left_panel.py:11-13
```

**CSS classes (no new CSS):**
- The status label uses the existing `dim-label` class.
- The popover inherits from GTK4 default popover styling.
- The menu rows are unstyled `Gtk.ListBoxRow`s, matching the `session_menu.py` pattern.

**Line count estimate:** ~110 lines added to `left_panel.py`.

---

### 2.2 `ui/views/left_panel.py` — store `_prompt_content` on the row

**What changes:** in `_build_prompt_row` (lines 663-714), set `row._prompt_content = prompt['content']` alongside the existing `row._filepath = prompt['filepath']` (line 666).

**Why:** Avoids a second disk read on every right-click. The `content` is already loaded by `PromptsHandler._scan_prompts` (verified at `prompts_handler.py:185` — `'content': text`). For prompts in the order of a few hundred KB this is acceptable; prompts are short-form system-prompt templates by design.

**Risk note:** If a prompt file is edited externally between the list refresh and the right-click, the in-memory copy will be stale. Acceptable for v1 — matches the "snapshot of what's listed" model. If the user wants the latest, they refresh the tab (search/refresh rebuilds the rows).

**Exact addition:**
```python
row._filepath = prompt['filepath']
row._name = prompt['name']
row._prompt_content = prompt['content']   # NEW — used by "Copy prompt" menu
```

---

### 2.3 `ui/handlers/prompts_handler.py` — no changes

**The handler is untouched.** All required data (`filepath`, `content`) is already produced by `load_prompts()` / `_scan_prompts()`. The view layer reads it via the `prompt` dict passed to `_build_prompt_row`.

**Verification (read of source):**
- `prompts_handler.py:185` — `'content': text` is set in `_scan_prompts` after `with open(fpath, 'r', encoding='utf-8') as f: text = f.read()`.
- `prompts_handler.py:187` — `'filepath': fpath` is the absolute path (verified: `_PROMPTS_DIR` is resolved via `os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))` at module import, then `os.path.join(prompts_dir, fname)` produces an absolute path).

---

### 2.4 Tests — `tests/test_left_panel.py` (new file) OR extend an existing file

**Decision:** Create a new file `tests/test_left_panel.py` because no test file for `left_panel.py` currently exists (verified: `ls tests/` shows no `test_left_panel.py`). This keeps the test surface focused and discoverable.

**Tests to add (each fully self-contained; uses real `LeftPanel` where GTK is available, mocks `Gdk.Display` for the clipboard calls):**

1. `test_prompt_row_has_filepath_and_content_attrs` — Verify `_build_prompt_row` sets `row._filepath` and `row._prompt_content` from the prompt dict.
2. `test_copy_path_calls_clipboard_with_filepath` — Patch `gi.repository.Gdk.Display.get_default`, call `_on_copy_prompt_path(row)`, verify `clipboard.set` was called with `row._filepath`.
3. `test_copy_prompt_calls_clipboard_with_content` — Patch `Gdk.Display.get_default`, call `_on_copy_prompt_content(row)`, verify `clipboard.set` was called with `row._prompt_content`.
4. `test_copy_path_skips_when_filepath_missing` — Row with no `_filepath` attr → no clipboard call, no exception.
5. `test_copy_prompt_skips_when_content_missing` — Row with no `_prompt_content` attr → no clipboard call, no exception.
6. `test_copy_status_label_shows_and_clears` — Verify `_show_prompt_copy_status("Copied path")` sets the label text and that the GLib timeout clears it after 2500ms (advance fake clock or call `_clear` directly).
7. `test_right_click_handler_ignores_multipress` — Call `_on_prompt_row_right_click(ctrl, n_press=2, ...)` and verify no popover is created (mock `Gtk.Popover` and assert not called).

**Test pattern (verified against `tests/test_feed_handler.py:420-435`):**
```python
from unittest.mock import MagicMock, patch

def test_copy_path_calls_clipboard_with_filepath(self, ...):
    with patch('gi.repository.Gdk.Display.get_default') as mock_display:
        mock_clipboard = MagicMock()
        mock_display.return_value.get_clipboard.return_value = mock_clipboard
        # ... build row, call handler
        mock_clipboard.set.assert_called_once_with("/abs/path/to/prompt.md")
```

**Line count estimate:** ~150 lines (new file).

---

### 2.5 `docs/ARCHITECTURE.md` — update §3.13 prompts_handler

**What changes:** add a note that `prompt['content']` is now also surfaced via the view layer for the right-click copy menu.

**Suggested text addition (after §3.13's current "Double-click or `+` button..." line at line 426):**

```
**Right-click menu (Phase 1):** Each prompt row also has a right-click popover with
"Copy path" and "Copy prompt" items. "Copy path" copies the absolute `.md` path;
"Copy prompt" copies the file contents (loaded by `_scan_prompts` into `prompt['content']`).
A transient label in the Prompts tab header confirms the copy.
```

---

## 3. Data Flow

### User action → UI handler → model/utility → result → UI update

```
[User right-clicks a prompt row]
        │
        ▼
[Gtk.GestureClick "pressed" signal fires]
        │
        ▼
LeftPanel._on_prompt_row_right_click(ctrl, n_press, x, y, row)        [left_panel.py, new]
        │   - check n_press == 1
        │   - check row._filepath exists
        │   - build Gtk.Popover with 2 Gtk.ListBoxRow items
        │   - popover.popup() at row
        │
        ▼
[User clicks "Copy path" or "Copy prompt" in the popover]
        │
        ▼
LeftPanel._on_prompt_menu_row_activated(lb, menu_row, popover, source_row)    [left_panel.py, new]
        │   - read menu_row child label text
        │   - popover.popdown(); popover.unparent()
        │   - dispatch: _on_copy_prompt_path(source_row) OR _on_copy_prompt_content(source_row)
        │
        ▼
LeftPanel._on_copy_prompt_path(row) OR _on_copy_prompt_content(row)          [left_panel.py, new]
        │   - read row._filepath / row._prompt_content
        │   - call _copy_text_to_clipboard(text)
        │   - call _show_prompt_copy_status("Copied path" or "Copied prompt")
        │
        ▼
LeftPanel._copy_text_to_clipboard(text)                                      [left_panel.py, new]
        │   - Gdk.Display.get_default() → .get_clipboard() → .set(text)
        │   - no return value
        │
        ▼
LeftPanel._show_prompt_copy_status(message)                                  [left_panel.py, new]
        │   - status_label.set_text(message)
        │   - GLib.timeout_add(2500, _clear)  →  clears label
        ▼
[Status label shows "Copied path" / "Copied prompt" for 2.5s, then clears]
```

**Key structures (all verified against source):**
- `row._filepath` — str, absolute path; set at `left_panel.py:666`.
- `row._prompt_content` — str, file contents; set at `left_panel.py:666` (new).
- `row._name` — str, filename without `.md` extension; set at `left_panel.py:667` (pre-existing, not used by this spec).
- `prompt['filepath']` — str; produced by `prompts_handler.py:_scan_prompts()` at line 187.
- `prompt['content']` — str; produced by `prompts_handler.py:_scan_prompts()` at line 185.

---

## 4. File Change Summary

| File | Change type | Lines (est.) | Risk |
|---|---|---|---|
| `ui/views/left_panel.py` | Modify (add right-click handler, status label, 2 new methods, store `_prompt_content`) | +120 / 0 | Low — additive, isolated to view layer |
| `ui/handlers/prompts_handler.py` | **No change** | 0 | None |
| `tests/test_left_panel.py` | New file | +150 | Low — mirrors existing `test_feed_handler.py` clipboard test pattern |
| `docs/ARCHITECTURE.md` | Modify (add right-click note to §3.13) | +5 | None |
| `utils/clipboard.py` | **No change** | 0 | None — no shared utility exists; we mirror the `chat_bubble._copy_to_clipboard` pattern inline (one new private method, well-tested) |

**Files NOT changed (already correct):**
- `ui/handlers/prompts_handler.py` — already exposes `filepath` and `content` in the prompt dict; no method changes needed. `get_prompt_content()` (line 117) is not used by this spec; we read `prompt['content']` from the row, which avoids a redundant disk read.
- `ui/views/chat_bubble.py` — its private `_copy_to_clipboard` (line 1004) is unchanged. We do not import it (it's a private symbol); we mirror its 5-line body in a new private method on `LeftPanel`. Sharing is a possible future refactor.
- `ui/views/session_menu.py` — unchanged. Its popover pattern is referenced for style consistency only; we do not depend on it.
- `ui/views/main_content.py` — unchanged. Its `_on_tab_right_click` (line 614) is referenced for the gesture-click pattern only.

---

## 5. Implementation Order

1. **Step 1** — Add `Gdk` import to `ui/views/left_panel.py:11-13`. Verify by running `grep -n "from gi.repository" ui/views/left_panel.py` and confirming 4 imports now (Gtk, Gio, GLib, Gdk).
2. **Step 2** — In `LeftPanel.__init__`, add `self._prompt_copy_status_label = None` and `self._prompt_copy_status_timeout_id = None` to the prompts-tab state block (~line 49).
3. **Step 3** — In `_build_prompts_tab`, append the status label to the header hbox (after the search entry, ~line 624). Verify by inspecting the header layout.
4. **Step 4** — In `_build_prompt_row`, add `row._prompt_content = prompt['content']` next to the existing `row._filepath = ...` line (~line 666).
5. **Step 5** — In `_build_prompt_row`, attach the right-click `Gtk.GestureClick` to the row after the `row.set_child(row_box)` call (~line 710).
6. **Step 6** — Add the new private methods to `LeftPanel`: `_on_prompt_row_right_click`, `_on_prompt_menu_row_activated`, `_on_copy_prompt_path`, `_on_copy_prompt_content`, `_copy_text_to_clipboard`, `_show_prompt_copy_status`. All methods are private (underscore prefix) and contained in the Prompts-tab section of the file.
7. **Step 7** — Run existing test suite to confirm no regressions: `cd /path/to/projects/crabcakes && pytest tests/ -q --tb=short`. **Verification gate:** must show all tests pass before proceeding.
8. **Step 8** — Create `tests/test_left_panel.py` with the 7 tests listed in §2.4.
9. **Step 9** — Run new test file: `cd /path/to/projects/crabcakes && pytest tests/test_left_panel.py -v`. **Verification gate:** 7 tests must pass.
10. **Step 10** — Update `docs/ARCHITECTURE.md` §3.13 with the right-click note.
11. **Step 11** — Run full test suite once more: `cd /path/to/projects/crabcakes && pytest tests/ -q --tb=short`. **Verification gate:** all tests pass.

---

## 6. Acceptance Criteria

- [ ] Right-clicking any prompt row in the Prompts tab opens a 2-item popover.
- [ ] Popover items: "Copy path" and "Copy prompt", in that order, top to bottom.
- [ ] Clicking "Copy path" puts the absolute `.md` path of that prompt on the system clipboard.
- [ ] Clicking "Copy prompt" puts the raw file contents (as read from disk at list-refresh time) on the system clipboard.
- [ ] After a successful copy, the Prompts tab header shows "Copied path" or "Copied prompt" for ~2.5 seconds, then clears.
- [ ] Right-clicking the `+ Add Prompt` card does **not** open the copy menu (it is a separate widget with no `_filepath`).
- [ ] No regression: all pre-existing tests in `tests/` pass.
- [ ] 7 new tests in `tests/test_left_panel.py` pass.
- [ ] No GTK imports added to `ui/handlers/prompts_handler.py`.
- [ ] Pango-escaped labels per MED-9 (verified: the menu labels are static strings "Copy path" / "Copy prompt" — no user input interpolated, so escaping is a no-op but the pattern is followed).

---

## 7. Edge Cases

| Case | Expected behavior |
|---|---|
| User right-clicks the `+ Add Prompt` card | No menu shown. (The `+` row is a separate `Gtk.ListBoxRow`; the gesture check `hasattr(row, "_filepath")` filters it out.) |
| User right-clicks with `n_press != 1` (e.g., double-right-click) | Popover not opened. (`if n_press != 1: return` matches `main_content.py:614-617` pattern.) |
| `row._filepath` is empty string | `_on_copy_prompt_path` early-returns (`if not filepath: return`). No clipboard call, no status shown. |
| `row._prompt_content` is `None` | `_on_copy_prompt_content` early-returns (`if content is None: return`). No clipboard call, no status shown. |
| File deleted from disk between list refresh and copy | `prompt['content']` is whatever was read at refresh time; clipboard receives the stale contents. Acceptable for v1. If a future spec wants fresh-on-copy, it can call `prompts_handler.get_prompt_content(row._filepath)` instead. |
| `Gdk.Display.get_default()` returns `None` (headless env) | `_copy_text_to_clipboard` early-returns. Status label still shows. No exception. |
| User spam-clicks two copies in quick succession | The status label is overwritten; the previous timeout is cancelled and a new 2.5s timer starts. The label always reflects the most recent copy. |
| User copies during the 2.5s status window, then right-clicks again | The 2.5s timer is cancelled by the new `_show_prompt_copy_status` call. The new status shows. |
| Prompt file is very large (e.g., 500KB system prompt) | The full content is held in memory on the row + copied to the clipboard. No streaming, no truncation. Matches the existing "all content in the prompt dict" model. |
| Right-clicking a row that's mid-search-filter (e.g., disappearing) | Popover is parented to the row widget. If the row is removed from the listbox during the popover's lifetime, GTK4 will dismiss the popover (default behavior). No crash. |

---

## 8. ARCHITECTURE.md Updates Required

| Section | Change |
|---|---|
| §3.13 `ui/handlers/prompts_handler.py` | Add "Right-click menu (Phase 1)" paragraph noting the popover, its 2 items, and the confirmation label. (See §2.5 above for the exact text.) |
| §11 (file inventory) | Add `tests/test_left_panel.py` as a new test file. |
| §4 (data flow) | Optional: add a 1-sentence note that the Prompts tab view now also serves the right-click copy path. Not required. |
