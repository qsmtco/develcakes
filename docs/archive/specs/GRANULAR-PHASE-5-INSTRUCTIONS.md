# GRANULAR Phase 5 of 8 — FeedTab Toolbar Rebuild

**Spec:** `docs/specs/SPEC-AUTO-ACCEPT-GRANULAR-1.md` §2.3
**File to change:** `ui/views/feed_tab.py` (433 lines)
**Builder prompt:** `prompts/steelFramedCodeWriter.md`

## CRITICAL: Read ALL files before starting

Before writing ANY code, READ these files completely:
1. `ui/views/feed_tab.py` — the ENTIRE file (433 lines)
2. `docs/specs/SPEC-AUTO-ACCEPT-GRANULAR-1.md` §2.3 — the spec section with exact code
3. `ui/handlers/feed_handler.py` — understand how FeedHandler calls FeedTab (Phase 4 output)
4. `prompts/steelFramedCodeWriter.md` — your standing orders

## Task

Replace the single `_auto_accept_toggle` with three toggle buttons, an agent dropdown, and a snooze button. Replace `update_auto_accept_state()` with `update_auto_accept_prefs()`. Add new callback setters.

There are 6 changes — ALL in `ui/views/feed_tab.py`:

### Change 1: Constructor — replace toolbar widgets

In `__init__`, find the toolbar section that creates `_auto_accept_toggle`:

```python
self._auto_accept_toggle = Gtk.ToggleButton(label="Auto-Accept: OFF")
self._auto_accept_toggle.add_css_class("feed-toolbar-toggle")
self._auto_accept_toggle.connect("toggled", self._on_auto_accept_toggled)
```

Replace with:

```python
# Group 1 — per-type toggles (v2 granular controls)
self._diffs_toggle = Gtk.ToggleButton(label="Diffs: OFF")
self._diffs_toggle.add_css_class("feed-toolbar-toggle")
self._diffs_toggle.add_css_class("feed-toolbar-toggle-per-type")
self._diffs_toggle.connect("toggled", self._on_diffs_toggled)

self._files_toggle = Gtk.ToggleButton(label="Files: OFF")
self._files_toggle.add_css_class("feed-toolbar-toggle")
self._files_toggle.add_css_class("feed-toolbar-toggle-per-type")
self._files_toggle.connect("toggled", self._on_files_toggled)

self._exec_toggle = Gtk.ToggleButton(label="Exec: OFF")
self._exec_toggle.add_css_class("feed-toolbar-toggle")
self._exec_toggle.add_css_class("feed-toolbar-toggle-per-type")
self._exec_toggle.connect("clicked", self._on_exec_clicked)

# Separator between toggle group and agent scope
self._scope_divider = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
self._scope_divider.add_css_class("feed-toolbar-divider")

# Group 2 — agent scope (placeholder; populated in later phase)
self._agent_dropdown = Gtk.DropDown()
self._agent_dropdown.add_css_class("feed-toolbar-agent-dropdown")

# Separator between scope and snooze
self._snooze_divider = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
self._snooze_divider.add_css_class("feed-toolbar-divider")

# Group 3 — snooze (hidden when count == 0)
self._snooze_button = Gtk.MenuButton(label="Snooze 0")
self._snooze_button.add_css_class("feed-toolbar-snooze")
self._snooze_button.set_visible(False)
```

Also add initialization for new state/callbacks:

```python
self._diffs_toggle_callback: Callable[[bool], None] | None = None
self._files_toggle_callback: Callable[[bool], None] | None = None
self._exec_toggle_callback: Callable[[str], None] | None = None
self._agent_scope_callback: Callable[[str], None] | None = None
self._exec_mode: str = "off"
```

### Change 2: Constructor — replace toolbar assembly

Find the `self._toolbar.append(...)` calls:

```python
self._toolbar.append(self._auto_accept_toggle)
self._toolbar.append(self._divider)
self._toolbar.append(self._batch_accept_button)
self._toolbar.append(self._batch_accept_label)
```

Replace with:

```python
self._toolbar.append(self._diffs_toggle)
self._toolbar.append(self._files_toggle)
self._toolbar.append(self._exec_toggle)
self._toolbar.append(self._scope_divider)
self._toolbar.append(self._agent_dropdown)
self._toolbar.append(self._snooze_divider)
self._toolbar.append(self._snooze_button)
self._toolbar.append(self._divider)             # existing divider
self._toolbar.append(self._batch_accept_button)  # existing
self._toolbar.append(self._batch_accept_label)   # existing
```

### Change 3: Add new callback setters

After the existing `set_auto_accept_callback` method, add:

```python
def set_diffs_toggle_callback(self, callback: Callable[[bool], None] | None) -> None:
    """Install callback for the Diffs toggle. Receives new active state."""
    self._diffs_toggle_callback = callback

def set_files_toggle_callback(self, callback: Callable[[bool], None] | None) -> None:
    """Install callback for the Files toggle. Receives new active state."""
    self._files_toggle_callback = callback

def set_exec_toggle_callback(self, callback: Callable[[str], None] | None) -> None:
    """Install callback for the Exec toggle. Receives new mode string."""
    self._exec_toggle_callback = callback

def set_agent_scope_callback(self, callback: Callable[[str], None] | None) -> None:
    """Install callback for the agent dropdown. Receives new scope string."""
    self._agent_scope_callback = callback
```

### Change 4: Add new toggle handlers + exec cycle

After the existing `_on_auto_accept_toggled` method, add:

```python
def _on_diffs_toggled(self, button: Gtk.ToggleButton) -> None:
    """Handler for the Diffs toggle's 'toggled' signal."""
    if self._diffs_toggle_callback is not None:
        self._diffs_toggle_callback(button.get_active())

def _on_files_toggled(self, button: Gtk.ToggleButton) -> None:
    """Handler for the Files toggle's 'toggled' signal."""
    if self._files_toggle_callback is not None:
        self._files_toggle_callback(button.get_active())

def _on_exec_clicked(self, button: Gtk.Button) -> None:
    """3-state cycle: OFF → SHOW → SILENT → OFF."""
    if self._exec_mode == "off":
        new_mode = "show"
    elif self._exec_mode == "show":
        new_mode = "silent"
    else:
        new_mode = "off"
    self._exec_mode = new_mode
    self._exec_toggle.set_label(f"Exec: {new_mode.upper()}")
    if self._exec_toggle_callback is not None:
        self._exec_toggle_callback(new_mode)
```

### Change 5: Add `update_auto_accept_prefs()` + legacy bridge

Replace the existing `update_auto_accept_state()` method with:

```python
def update_auto_accept_prefs(self, prefs_dict: dict) -> None:
    """Reconcile all toolbar visuals from the v2 prefs dict.

    Called by FeedHandler whenever prefs change. The view never owns state;
    it only reflects what the handler tells it.

    Args:
        prefs_dict: A v2 prefs dict (as produced by AutoAcceptPrefs.to_dict()
            or load_feed_prefs()). Must have version == 2.
    """
    auto = prefs_dict.get("auto_accept", {})
    fc = auto.get("file_changes", {})

    # Diffs toggle
    diff_enabled = fc.get("diff", {}).get("enabled", False)
    self._diffs_toggle.set_active(diff_enabled)
    self._diffs_toggle.set_label(f"Diffs: {'ON' if diff_enabled else 'OFF'}")

    # Files toggle
    files_enabled = any(
        fc.get(ct, {}).get("enabled", False)
        for ct in ("file_created", "file_modified", "file_deleted")
    )
    self._files_toggle.set_active(files_enabled)
    self._files_toggle.set_label(f"Files: {'ON' if files_enabled else 'OFF'}")

    # Exec toggle (3-state)
    exec_mode = auto.get("exec_command", {}).get("mode", "off")
    self._exec_mode = exec_mode
    self._exec_toggle.set_label(f"Exec: {exec_mode.upper()}")

    # Snooze count
    snoozed = auto.get("snoozed_card_ids", [])
    count = len(snoozed)
    self._snooze_button.set_label(f"Snooze {count}")
    self._snooze_button.set_visible(count > 0)

def update_auto_accept_state(self, active: bool) -> None:
    """Legacy bridge — constructs a prefs dict from the single-toggle state.

    Deprecated: use update_auto_accept_prefs() directly. Preserves v1
    semantics: ALL four file-change types enabled together with
    agent_scope='all_agents' to preserve v1 test semantics.
    """
    prefs = {
        "version": 2,
        "auto_accept": {
            "file_changes": {
                ct: {"enabled": active, "agent_scope": "all_agents"}
                for ct in ("diff", "file_created", "file_modified", "file_deleted")
            },
            "exec_command": {"mode": "off", "agent_scope": "all_agents"},
            "snoozed_card_ids": [],
        },
    }
    self.update_auto_accept_prefs(prefs)
```

### Change 6: Add `hide_card_buttons()` method

Add this method to the class:

```python
def hide_card_buttons(self, card_id: str, button_names: list[str]) -> None:
    """Hide specific action buttons on a card widget.

    Used by FeedHandler._auto_approve_exec_card() in Show mode to hide
    the Approve/Deny buttons on cards that were auto-approved.

    Args:
        card_id: The card whose buttons should be hidden.
        button_names: List of button names to hide (e.g. ["approve", "deny"]).
    """
    card_widget = self._cards_by_id.get(card_id)
    if card_widget is None:
        return
    for name in button_names:
        btn = getattr(card_widget, f"_{name}_button", None)
        if btn is not None:
            btn.set_visible(False)
```

### DO NOT:
- Remove the existing `set_auto_accept_callback` method (legacy compat)
- Remove the existing `_on_auto_accept_toggled` handler (legacy compat)
- Remove the existing `_auto_accept_callback` attribute (legacy compat)
- Modify any other files
- Add tests (Phase 8)

## IMPORTANT: MockFeedTab compatibility

The existing tests use a `MockFeedTab` that defines `set_auto_accept_callback` and `update_auto_accept_state`. The real `FeedTab` changes here do NOT affect the mock — tests don't import the real `FeedTab`.

## Verification

```bash
# Verify the file parses
python3 -c "import ast; ast.parse(open('ui/views/feed_tab.py').read()); print('AST OK')"

# Verify new methods exist
grep -n "def set_diffs_toggle_callback\|def set_files_toggle_callback\|def set_exec_toggle_callback\|def set_agent_scope_callback\|def _on_diffs_toggled\|def _on_files_toggled\|def _on_exec_clicked\|def update_auto_accept_prefs\|def hide_card_buttons" ui/views/feed_tab.py

# Verify old methods still exist (legacy compat)
grep -n "def set_auto_accept_callback\|def update_auto_accept_state\|def _on_auto_accept_toggled" ui/views/feed_tab.py

# Verify old toggle removed
grep -c "_auto_accept_toggle" ui/views/feed_tab.py  # should be 0

# Verify new widgets exist
grep -c "_diffs_toggle\|_files_toggle\|_exec_toggle\|_agent_dropdown\|_snooze_button" ui/views/feed_tab.py

# Run ALL tests
python3 -m pytest tests/test_feed_handler.py tests/test_feed_card.py tests/test_feed_store.py -q

# Line count
wc -l ui/views/feed_tab.py
```

## COMPLETENESS checklist

```
COMPLETENESS:
- [x/not done] _auto_accept_toggle removed — evidence (grep count = 0)
- [x/not done] _diffs_toggle + _files_toggle + _exec_toggle added — evidence (grep)
- [x/not done] _agent_dropdown + _snooze_button added — evidence (grep)
- [x/not done] Callback setters added (4 new) — evidence (grep)
- [x/not done] _on_diffs_toggled + _on_files_toggled + _on_exec_clicked handlers added — evidence (grep)
- [x/not done] update_auto_accept_prefs() added — evidence (grep)
- [x/not done] update_auto_accept_state() kept as legacy bridge — evidence (grep)
- [x/not done] hide_card_buttons() added — evidence (grep)
- [x/not done] set_auto_accept_callback + _on_auto_accept_toggled kept (legacy) — evidence (grep)
- [x/not done] All existing tests pass — evidence (pytest output)
```
