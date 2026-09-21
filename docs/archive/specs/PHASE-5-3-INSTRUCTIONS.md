# Phase 5-3 — FeedHandler Auto-Accept State + Hook + Prefs Load

> Part of FEED-CARD-UX-PHASE-5 — Persistent Feed Toolbar + Auto-Accept Toggle
> Implements spec Steps 5 and 6.

## Before Starting

1. Read the full master spec: `docs/specs/FEED-CARD-UX-PHASE-5-INSTRUCTIONS.md`
2. Read the steelFramedCodeWriter prompt: `prompts/steelFramedCodeWriter.md`
3. Read the full file you will edit (`ui/handlers/feed_handler.py`) before touching it.

## File to Change: `ui/handlers/feed_handler.py`

All edits in this single file. The spec says Steps 5 and 6 are both in FeedHandler, so this is one file, multiple edits.

### Edit A: Module-level constant (near top of file, after imports)

After the existing `_VALID_SHA_RE` line (line ~16), add:
```python
_AUTO_ACCEPT_TYPES = {"diff", "file_created", "file_modified", "file_deleted"}
```

### Edit B: New instance vars in `__init__`

After `self._echo_suppress_seconds = 3.0` (around line 79), add:
```python
        # Phase 5: auto-accept toggle state
        self._auto_accept_enabled: bool = False
        self._auto_accept_agent: str | None = None
        self._show_auto_accept_warning: Callable | None = None  # callback injected by Window
```

### Edit C: `set_show_auto_accept_warning` method

Add after `set_feed_tab` method (after line ~113):
```python
    def set_show_auto_accept_warning(self, callback: Callable | None) -> None:
        """
        Install the callback invoked when the user toggles auto-accept ON.
        The callback receives (agent_name: str, on_confirm: Callable, on_cancel: Callable).
        Pass None to clear. Called by Window after FeedHandler is constructed. (Phase 5)
        """
        self._show_auto_accept_warning = callback
```

### Edit D: Wire auto-accept callback in `set_feed_tab`

In `set_feed_tab` (line ~96), after the existing `self._feed_tab.set_batch_accept_callback(...)` call, add:
```python
            # Phase 5: wire auto-accept toggle callback
            self._feed_tab.set_auto_accept_callback(self._on_auto_accept_toggled)
```

### Edit E: `_resolve_agent_name_for_dialog` method

Add after `set_show_auto_accept_warning`:
```python
    def _resolve_agent_name_for_dialog(self) -> str:
        """
        Return the best human-readable agent name for the warning dialog.
        Fallback chain: _auto_accept_agent → most recent card's author → "the active agent".
        Never returns None or the string "None". (Phase 5)
        """
        if self._auto_accept_agent:
            return self._auto_accept_agent
        # Iterate cards newest-first, find most recent card with an author
        if self._active_project_name:
            card_ids = self._project_cards.get(self._active_project_name, [])
            for cid in card_ids:  # newest first
                card = self._cards.get(cid)
                if card and card.author:
                    return card.author
        return "the active agent"
```

### Edit F: `_on_auto_accept_toggled`, `_enable_auto_accept`, `_cancel_auto_accept`, `_disable_auto_accept`

Add after `_resolve_agent_name_for_dialog`:
```python
    def _on_auto_accept_toggled(self, active: bool) -> None:
        """
        Called when the user clicks the auto-accept toggle.
        ON: show warning dialog (if callback wired), then enable.
        OFF: disable immediately (no dialog needed). (Phase 5)
        """
        if active:
            if self._show_auto_accept_warning is not None:
                self._show_auto_accept_warning(
                    self._resolve_agent_name_for_dialog(),
                    on_confirm=self._enable_auto_accept,
                    on_cancel=self._cancel_auto_accept,
                )
            else:
                # No warning callback wired (tests, headless) — enable directly
                self._enable_auto_accept()
        else:
            self._disable_auto_accept()

    def _enable_auto_accept(self) -> None:
        """Enable auto-accept and persist state. (Phase 5)"""
        self._auto_accept_enabled = True
        self._GLib.idle_add(self._save_feed_prefs_idle)

    def _cancel_auto_accept(self) -> None:
        """Visually snap the toggle back to OFF without persisting. (Phase 5)"""
        if self._feed_tab is not None:
            self._GLib.idle_add(lambda: self._feed_tab.update_auto_accept_state(False))

    def _disable_auto_accept(self) -> None:
        """Disable auto-accept and persist state. (Phase 5)"""
        self._auto_accept_enabled = False
        self._GLib.idle_add(self._save_feed_prefs_idle)
```

### Edit G: `_save_feed_prefs_idle` method

Add after `_disable_auto_accept`:
```python
    def _save_feed_prefs_idle(self) -> None:
        """
        Persist auto-accept state to .crabcakes/feed-prefs.json. (Phase 5)
        Called via GLib.idle_add so it runs on the main thread.
        """
        project_path = self._project_paths.get(self._active_project_name or "")
        if not project_path:
            return
        feed_store.save_feed_prefs(project_path, {
            "version": 1,
            "auto_accept_enabled": self._auto_accept_enabled,
            "auto_accept_agent": self._auto_accept_agent,
        })
```

### Edit H: Auto-accept check inside `_append` closure in `add_card`

In `add_card` (line ~114), find the `_append` closure (starts around line 185). Inside it, after `self._feed_tab.append_card(widget, card_id)` and `self._schedule_smart_scroll()` and BEFORE `if self._on_card_added:`, add:

```python
                # Phase 5: auto-accept check (runs on main thread via idle_add)
                if (self._auto_accept_enabled
                        and card_data.accepted is None
                        and card_data.card_type in _AUTO_ACCEPT_TYPES
                        and (self._auto_accept_agent is None or card_data.author == self._auto_accept_agent)):
                    # Lazy agent lock-in: first card after toggle ON sets the agent
                    if self._auto_accept_agent is None and card_data.author:
                        self._auto_accept_agent = card_data.author
                        self._GLib.idle_add(self._save_feed_prefs_idle)
                    self._GLib.idle_add(lambda cid=card_data.card_id: self.handle_accept(cid))
```

**Critical:** The auto-accept check MUST go INSIDE the `_append` closure, NOT in the main body of `add_card`. The `_append` closure runs on the main thread via `GLib.idle_add`. The check must run after `append_card` so the widget exists in the tree before `handle_accept` starts git ops.

### Edit I: Load prefs in `on_project_opened` — inside `_load_and_render`

In `on_project_opened` (line ~475), inside `_load_and_render`, after `cards = feed_store.load_feed(project_path)` (around line 499), add:
```python
            # Phase 5: load auto-accept prefs
            prefs = feed_store.load_feed_prefs(project_path)
            self._auto_accept_enabled = prefs.get("auto_accept_enabled", False)
            self._auto_accept_agent = prefs.get("auto_accept_agent")
```

### Edit J: Apply prefs in `_append_and_schedule_scroll`

In `_append_and_schedule_scroll` (around line 583), after the `self._schedule_smart_scroll()` call and before `return False`, add:
```python
                # Phase 5: apply persisted auto-accept state to toggle visual
                if self._auto_accept_enabled is not None:
                    self._feed_tab.update_auto_accept_state(self._auto_accept_enabled)
```

## Rules

- Use the steelFramedCodeWriter prompt at `prompts/steelFramedCodeWriter.md`
- Do NOT modify any files other than `ui/handlers/feed_handler.py`
- Do NOT add tests — tests are in a later phase
- Do NOT modify `tests/test_low2_file_sandbox.py`
- Report: file locations with line numbers, grep evidence for all new methods/vars, test results
- Include a COMPLETENESS checklist with evidence for each edit

## Verify

1. `python3 -c "import ui.handlers.feed_handler"` — no import errors
2. `grep -n "_auto_accept\|_AUTO_ACCEPT\|_save_feed_prefs_idle\|_resolve_agent_name" ui/handlers/feed_handler.py` — all new identifiers present
3. `grep -n "_append" ui/handlers/feed_handler.py` — confirm auto-accept check is inside `_append` closure (line number AFTER `def _append`)
4. `pytest tests/test_feed_handler.py -q --tb=short` — all pass (auto-accept is OFF by default, so no behavior change)
5. `pytest tests/test_feed_store.py tests/test_feed_handler.py tests/test_feed_card.py tests/test_review_handler_feed_card.py tests/test_low12_13_feed.py -q --tb=short` — all pass
