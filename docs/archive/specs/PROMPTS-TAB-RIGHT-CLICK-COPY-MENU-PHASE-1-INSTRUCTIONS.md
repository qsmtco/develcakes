# Phase 1 Instructions — Prompts Tab Right-Click Copy Menu (Code Changes Only)

**Spec:** `docs/specs/PROMPTS-TAB-RIGHT-CLICK-COPY-MENU-PHASE-1-SPEC.md` (read this in full before starting)
**Channel:** Authorized (Captain confirmed in pre-loop conversation)

## READ THESE FILES IN FULL BEFORE WRITING ANY CODE

> Per steelFramedCodeWriter Rule 1 — read every file before touching it.

1. `ui/views/left_panel.py` (read the WHOLE file; the view is 800+ lines and the changes touch multiple regions)
2. `docs/specs/PROMPTS-TAB-RIGHT-CLICK-COPY-MENU-PHASE-1-SPEC.md` (the full spec)
3. `docs/ARCHITECTURE.md` (especially §3.13 — prompts_handler; §4.2 — popover-menu pattern; §11 — file inventory)

Reference patterns (read for context, do NOT modify):
- `ui/views/chat_bubble.py:1004-1010` — `_copy_to_clipboard` (clipboard pattern to mirror)
- `ui/views/main_content.py:353-365` and `_on_tab_right_click` at line 614 — right-click gesture pattern
- `ui/views/session_menu.py:38-90` — popover-menu UI pattern
- `ui/views/file_tree.py:308-332` — popover with button pattern
- `ui/handlers/prompts_handler.py:154-195` — `_scan_prompts` (where `prompt['content']` and `prompt['filepath']` originate)
- `tests/test_feed_handler.py:420-435` — `TestHandleCopy` (clipboard mock pattern for tests later, not now)

## TASK — CODE CHANGES ONLY (no tests, no docs in this phase)

### Edit 1: Add `Gdk` to the existing `gi.repository` import

File: `ui/views/left_panel.py`
Current line 9: `from gi.repository import Gtk, Gio, GLib`
Change to: `from gi.repository import Gtk, Gio, GLib, Gdk`

Why: `Gdk.BUTTON_SECONDARY` is needed for the right-click gesture. Verified: Gdk is not currently imported (only Gtk, Gio, GLib are).

### Edit 2: Add two new state attrs to `LeftPanel.__init__`

File: `ui/views/left_panel.py`
Find the prompts-tab state block in `__init__` (search for `self._prompts_handler = None` to locate it).
After that line, add:
```python
self._prompt_copy_status_label = None      # built in _build_prompts_tab
self._prompt_copy_status_timeout_id = None  # GLib source id for the auto-clear
```

### Edit 3: Store `_prompt_content` on the row

File: `ui/views/left_panel.py`, function `_build_prompt_row` (line ~670)
Find:
```python
row = Gtk.ListBoxRow()
row._filepath = prompt['filepath']
row._name = prompt['name']
```
Change to:
```python
row = Gtk.ListBoxRow()
row._filepath = prompt['filepath']
row._name = prompt['name']
row._prompt_content = prompt['content']   # NEW — used by "Copy prompt" menu
```

### Edit 4: Append the status label to the Prompts tab header

File: `ui/views/left_panel.py`, function `_build_prompts_tab` (line ~598)
Find the block that builds the header (search for `header.append(search_entry)` and the assignment to `self._prompts_tab_header = header`).
AFTER `header.append(search_entry)` and BEFORE `self._prompts_tab_header = header`, insert:
```python
        # Transient copy-status label (right-aligned in the header).
        status_label = Gtk.Label(label="")
        status_label.set_valign(Gtk.Align.CENTER)
        status_label.set_margin_start(4)
        status_label.set_margin_end(4)
        status_label.add_css_class("dim-label")
        status_label.set_xalign(1.0)
        header.append(status_label)
        self._prompt_copy_status_label = status_label
```

### Edit 5: Attach a right-click gesture to the row

File: `ui/views/left_panel.py`, function `_build_prompt_row` (line ~670)
Find the end of the function (look for `row.set_child(row_box)` and the return statement). AFTER `row.set_child(row_box)` and BEFORE `return row`, insert:
```python
        # Right-click → context menu (Copy path / Copy prompt).
        right_ctrl = Gtk.GestureClick()
        right_ctrl.set_button(Gdk.BUTTON_SECONDARY)
        right_ctrl.connect("pressed", self._on_prompt_row_right_click, row)
        row.add_controller(right_ctrl)
```

### Edit 6: Add 6 new private methods to `LeftPanel`

File: `ui/views/left_panel.py`
Append these 6 methods to the `LeftPanel` class (anywhere after `_on_prompt_row_activated` is fine — placing them adjacent to related prompt-row methods is preferred for readability). All method names start with `_` (private).

```python
    # ── Right-click copy menu (Phase 1) ───────────────────────────────────

    def _on_prompt_row_right_click(self, ctrl, n_press, x, y, row) -> None:
        """
        Right-click on a prompt row — show the 2-item copy menu.

        Args:
            ctrl:    Gtk.GestureClick (sender, not used).
            n_press: int — number of presses (only respond to single click).
            x, y:    float — local click coordinates (unused; popover anchors to row).
            row:     Gtk.ListBoxRow — the right-clicked row (carries _filepath + _prompt_content).
        """
        if n_press != 1:
            return
        if not hasattr(row, "_filepath"):
            return  # defensive: skip non-prompt rows (e.g., the "+ Add" row)

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

    def _on_prompt_menu_row_activated(self, _lb, menu_row, popover, source_row) -> None:
        """
        Dispatch the selected menu action and dismiss the popover.

        The source_row is the original prompt row (carries _filepath and _prompt_content).
        We identify the action by reading the child label text — labels are static
        literal strings "Copy path" / "Copy prompt" (MED-9-safe: no user input interpolated).
        """
        label_widget = menu_row.get_child()
        action = label_widget.get_text() if label_widget is not None else ""
        popover.popdown()
        popover.unparent()
        if action == "Copy path":
            self._on_copy_prompt_path(source_row)
        elif action == "Copy prompt":
            self._on_copy_prompt_content(source_row)

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

## RULES (per steelFramedCodeWriter)

- Use the prompt at `prompts/steelFramedCodeWriter.md`. Follow every rule, especially Rule 1 (read every file before touching), Rule 3 (verify every claim against source — run `grep`/`wc -l` for line counts and identifiers), and Rule 8 (do not modify what you weren't asked to modify).
- Start with a Discovery block listing every file you read.
- Hard-part first: write the new methods (Edit 6) and verify the file still imports cleanly before wiring them up. Then wire (Edit 5).
- Maximum 15 lines before stopping to verify.
- Do NOT create tests in this phase. Do NOT update ARCHITECTURE.md in this phase. Tests come in Phase 2, docs in Phase 3.
- MED-9: the menu labels are static literal strings ("Copy path", "Copy prompt"). No Pango interpolation → no `GLib.markup_escape_text()` needed (no-op pattern, documented in spec).

## VERIFICATION (must run yourself, paste output in report)

```bash
# 1. File still parses (no syntax errors)
cd /path/to/projects/crabcakes && python3 -c "import ast; ast.parse(open('ui/views/left_panel.py').read()); print('OK')"

# 2. Gdk import present
cd /path/to/projects/crabcakes && grep -n "^from gi.repository" ui/views/left_panel.py

# 3. New method names present
cd /path/to/projects/crabcakes && grep -n "_on_prompt_row_right_click\|_on_prompt_menu_row_activated\|_on_copy_prompt_path\|_on_copy_prompt_content\|_copy_text_to_clipboard\|_show_prompt_copy_status" ui/views/left_panel.py

# 4. _prompt_content set on row
cd /path/to/projects/crabcakes && grep -n "_prompt_content" ui/views/left_panel.py

# 5. Existing tests still pass (regression check)
cd /path/to/projects/crabcakes && pytest tests/ -q --tb=short

# 6. prompts_handler.py is UNCHANGED (no GTK imports added there)
cd /path/to/projects/crabcakes && grep -n "^from gi.repository\|^import gi" ui/handlers/prompts_handler.py

# 7. Line count of left_panel.py (paste the actual number)
cd /path/to/projects/crabcakes && wc -l ui/views/left_panel.py
```

## REPORT BACK

Reply in chat with:
1. **Files changed** (list each path, e.g. `ui/views/left_panel.py`)
2. **Verification outputs** (paste the actual output of all 7 commands above — not a paraphrase)
3. **Related-issue scan** (per steelFramedCodeWriter Step 6.6): scan the functions you modified for same-class bugs. Report as `Related issue found, not fixed: <description>` or `No related issues found.`
4. **Spec drift** (per steelFramedCodeWriter Step 6.8): if any line numbers in the spec/instructions were off by >10 lines, flag as `Spec drift: <description>`. Otherwise `No spec drift.`
5. **COMPLETENESS checklist** (literal block):
   ```
   COMPLETENESS:
   - [x] Edit 1: <description> — evidence (line N)
   - [x] Edit 2: <description> — evidence
   ...
   - [x] Edit 6: <description> — evidence
   ```

Use the word marker "please write" in your reply so I can confirm the message is canonical.

— Qaster (implementation supervisor)
