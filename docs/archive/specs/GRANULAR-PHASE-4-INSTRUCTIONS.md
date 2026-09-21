# GRANULAR Phase 4 of 8 — FeedHandler State Migration

**Spec:** `docs/specs/SPEC-AUTO-ACCEPT-GRANULAR-1.md` §2.4
**File to change:** `ui/handlers/feed_handler.py`
**Builder prompt:** `prompts/steelFramedCodeWriter.md`

## CRITICAL: Read ALL files before starting

Before writing ANY code, READ these files completely:
1. `ui/handlers/feed_handler.py` — the ENTIRE file (1382+ lines)
2. `docs/specs/SPEC-AUTO-ACCEPT-GRANULAR-1.md` §2.4 — the spec section with exact code
3. `models/feed_card.py` — Phase 1 output (AutoAcceptPrefs dataclass)
4. `utils/feed_store.py` — Phase 2 output (load_feed_prefs returns v2 dict)
5. `prompts/steelFramedCodeWriter.md` — your standing orders

## Task

This is the largest phase. You are modifying `ui/handlers/feed_handler.py` to replace the single-toggle auto-accept state with the v2 `AutoAcceptPrefs` system. There are 10 changes:

### Change 1: Remove `_AUTO_ACCEPT_TYPES` constant (line 25)

Delete the line: `_AUTO_ACCEPT_TYPES = {"diff", "file_created", "file_modified", "file_deleted"}`

### Change 2: Update `__init__` state (lines 102-108)

Replace the three state fields:
```python
self._auto_accept_enabled: bool = False
self._auto_accept_agent: str | None = None
```

With:
```python
from models.feed_card import AutoAcceptPrefs

# V2 auto-accept preferences (replaces _auto_accept_enabled + _auto_accept_agent)
self._prefs: AutoAcceptPrefs = AutoAcceptPrefs()
# Derived flag for legacy external callers and hot-path optimization
self._auto_accept_enabled: bool = False  # derives from prefs.any_enabled()
# Agent lock-in for first_author scope (runtime state, not persisted)
self._auto_accept_agent: str | None = None
```

**IMPORTANT:** The import of `AutoAcceptPrefs` should be at the top of the file with other imports, NOT inside `__init__`. Add it to the existing `from models.feed_card import ...` line. Do NOT add a duplicate import inside `__init__`.

### Change 3: Replace `set_feed_tab()` (lines 111-125)

Replace the existing method to wire new callbacks:
```python
def set_feed_tab(self, feed_tab) -> None:
    self._feed_tab = feed_tab
    if self._feed_tab is not None:
        self._feed_tab.set_batch_accept_callback(
            lambda: self._on_batch_accept_clicked()
        )
        # V2: wire per-toggle callbacks
        self._feed_tab.set_diffs_toggle_callback(self._on_diffs_toggled)
        self._feed_tab.set_files_toggle_callback(self._on_files_toggled)
        self._feed_tab.set_exec_toggle_callback(self._on_exec_toggled)
        # Keep legacy callback for backward compat during transition
        self._feed_tab.set_auto_accept_callback(self._on_auto_accept_toggled)
```

### Change 4: Update `set_show_auto_accept_warning()` (line 127)

Replace the docstring to document the v2 callback signature:
```python
def set_show_auto_accept_warning(self, callback: Callable | None) -> None:
    """
    Install the callback invoked when the user activates auto-accept
    for any feature (diffs, files, exec). (Phase 5 + v2)

    The callback signature (as of v2) is:
        callback(category: str, agent_name: str,
                 on_confirm: Callable, on_cancel: Callable)
    where category is one of "diffs" | "files" | "exec" and agent_name
    is the human-readable name of the agent the auto-accept applies to.

    Pass None to clear. Called by Window after FeedHandler is constructed.
    """
    self._show_auto_accept_warning = callback
```

### Change 5: Replace toggle methods (lines 152-209)

Replace `_on_auto_accept_toggled`, `_enable_auto_accept`, `_disable_auto_accept`, `_cancel_auto_accept` with the new per-toggle methods from spec §2.4:

- `_on_diffs_toggled(self, active: bool)`
- `_enable_diffs(self)`
- `_cancel_diffs(self)`
- `_on_files_toggled(self, active: bool)`
- `_enable_files(self)`
- `_cancel_files(self)`
- `_on_exec_toggled(self, mode: str)`
- `_confirm_exec_mode(self, mode: str)`
- `_refresh_auto_accept_state(self)`

KEEP the old `_on_auto_accept_toggled`, `_enable_auto_accept`, `_disable_auto_accept`, `_cancel_auto_accept` methods as stubs that call the legacy FeedTab callback path. They are still referenced by existing tests. The stubs should simply log a deprecation warning and delegate to `_refresh_auto_accept_state()`.

**Wait** — re-read the spec carefully. It says "Replace" the old methods. But the tests still use them. Check: do the existing tests test these methods directly? If so, we need to keep them as thin wrappers.

**Decision:** Keep the old methods as thin wrappers that map to the new system:
- `_on_auto_accept_toggled(True)` → calls `_on_diffs_toggled(True)` + `_on_files_toggled(True)`
- `_on_auto_accept_toggled(False)` → disables all
- `_enable_auto_accept()` → enables diffs + files
- `_disable_auto_accept()` → disables all
- `_cancel_auto_accept()` → disables all

This preserves backward compatibility with existing tests during the phased migration.

### Change 6: Replace `_save_feed_prefs_idle()` (lines 211-225)

```python
def _save_feed_prefs_idle(self) -> None:
    """Persist v2 auto-accept prefs to feed-prefs.json."""
    project_path = self._project_paths.get(self._active_project_name or "")
    if not project_path:
        return
    feed_store.save_feed_prefs(project_path, self._prefs.to_dict())
```

### Change 7: Update `add_card()` auto-accept guard (around line 307)

Replace:
```python
if (self._auto_accept_enabled
        and card_data.accepted is None
        and card_data.card_type in _AUTO_ACCEPT_TYPES
        and (self._auto_accept_agent is None or card_data.author == self._auto_accept_agent)):
```

With:
```python
if (card_data.accepted is None
        and self._is_card_auto_acceptable(card_data)):
```

Also remove the lazy lock-in code that follows (lines 312-314):
```python
if self._auto_accept_agent is None and card_data.author:
    self._auto_accept_agent = card_data.author
    self._GLib.idle_add(self._save_feed_prefs_idle)
```

The lazy lock-in is now handled inside `_agent_scope_matches()`.

### Change 8: Add new policy methods

Add these new methods after the toggle methods:

- `_is_card_auto_acceptable(self, card: FeedCardData) -> bool`
- `_agent_scope_matches(self, scope: str, author: str) -> bool`
- `snooze_card(self, card_id: str)`
- `unsnooze_card(self, card_id: str)`
- `_resolve_agent_name_for_dialog(self) -> str`

Copy the code verbatim from spec §2.4.

### Change 9: Update `on_project_opened()` (lines 633-634)

Replace:
```python
self._auto_accept_enabled = prefs.get("auto_accept_enabled", False)
self._auto_accept_agent = prefs.get("auto_accept_agent")
```

With:
```python
self._prefs = AutoAcceptPrefs.from_dict(prefs_raw)
self._auto_accept_enabled = self._prefs.any_enabled()
self._auto_accept_agent = None  # Reset lock-in on project open
```

**Note:** The variable that receives `load_feed_prefs()` result should be renamed from `prefs` to `prefs_raw` to avoid confusion with `self._prefs`.

### Change 10: Update `_append_and_schedule_scroll()` (lines 735-736)

Replace:
```python
if self._auto_accept_enabled is not None:
    self._feed_tab.update_auto_accept_state(self._auto_accept_enabled)
```

With:
```python
self._feed_tab.update_auto_accept_prefs(self._prefs.to_dict())
```

### DO NOT:
- Modify any other handlers
- Add exec auto-accept integration yet (that's Phase 6)
- Modify FeedTab (that's Phase 5)
- Remove the `_on_auto_accept_toggled` method — keep it as a legacy wrapper

## Verification

```bash
# Verify the file parses
python3 -c "import ast; ast.parse(open('ui/handlers/feed_handler.py').read()); print('AST OK')"

# Run existing tests — they must ALL still pass
python3 -m pytest tests/test_feed_handler.py -v

# Verify key methods exist
grep -n "def _on_diffs_toggled\|def _on_files_toggled\|def _on_exec_toggled\|def _is_card_auto_acceptable\|def _agent_scope_matches\|def _refresh_auto_accept_state\|def snooze_card\|def unsnooze_card\|def _resolve_agent_name_for_dialog\|def _save_feed_prefs_idle" ui/handlers/feed_handler.py

# Verify old constants removed
grep -n "_AUTO_ACCEPT_TYPES" ui/handlers/feed_handler.py

# Verify AutoAcceptPrefs import added
grep -n "AutoAcceptPrefs" ui/handlers/feed_handler.py

# Line count
wc -l ui/handlers/feed_handler.py
```

## COMPLETENESS checklist

```
COMPLETENESS:
- [x/not done] _AUTO_ACCEPT_TYPES removed — evidence (grep shows no results)
- [x/not done] self._prefs: AutoAcceptPrefs added to __init__ — evidence (grep)
- [x/not done] AutoAcceptPrefs imported at file top — evidence (grep)
- [x/not done] set_feed_tab() wires new callbacks — evidence (code)
- [x/not done] set_show_auto_accept_warning docstring updated — evidence (code)
- [x/not done] _on_diffs_toggled/_enable_diffs/_cancel_diffs added — evidence (grep)
- [x/not done] _on_files_toggled/_enable_files/_cancel_files added — evidence (grep)
- [x/not done] _on_exec_toggled/_confirm_exec_mode added — evidence (grep)
- [x/not done] _refresh_auto_accept_state() added — evidence (grep)
- [x/not done] Old toggle methods kept as legacy wrappers — evidence (code)
- [x/not done] _save_feed_prefs_idle() uses v2 format — evidence (code)
- [x/not done] add_card() uses _is_card_auto_acceptable() — evidence (code)
- [x/not done] _is_card_auto_acceptable() added — evidence (grep)
- [x/not done] _agent_scope_matches() added — evidence (grep)
- [x/not done] snooze_card()/unsnooze_card() added — evidence (grep)
- [x/not done] _resolve_agent_name_for_dialog() added — evidence (grep)
- [x/not done] on_project_opened() loads v2 prefs — evidence (code)
- [x/not done] _append_and_schedule_scroll uses update_auto_accept_prefs — evidence (code)
- [x/not done] All existing tests pass — evidence (pytest output)
```
