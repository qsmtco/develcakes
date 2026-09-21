# Phase 5-4 — Window Warning Dialog Wiring

> Part of FEED-CARD-UX-PHASE-5 — Persistent Feed Toolbar + Auto-Accept Toggle
> Implements spec Step 7.

## Before Starting

1. Read the full master spec: `docs/specs/FEED-CARD-UX-PHASE-5-INSTRUCTIONS.md`
2. Read the steelFramedCodeWriter prompt: `prompts/steelFramedCodeWriter.md`
3. Read the full file you will edit (`ui/window.py`) before touching it.

## File to Change: `ui/window.py`

### Edit 0: Add `Callable` import

At the top of the file, find the existing imports (after `import logging`, around line 10-15). Add:
```python
from typing import Callable
```

If there's already a `from typing import ...` line, add `Callable` to it instead.

### Edit A: Wire `set_show_auto_accept_warning` after `set_feed_tab`

After the line `self._feed_handler.set_feed_tab(self._feed_tab)` (around line 473), add:

```python
        # Phase 5: wire auto-accept warning dialog callback
        self._feed_handler.set_show_auto_accept_warning(
            lambda agent_name, on_confirm, on_cancel: self._show_auto_accept_warning(
                agent_name, on_confirm, on_cancel
            )
        )
```

### Edit B: Add `_show_auto_accept_warning` method

Add a new method after `_on_agent_selected` (after line ~910). Follow the existing dialog pattern (like `_open_agent_builder`):

```python
    def _show_auto_accept_warning(
        self,
        agent_name: str,
        on_confirm: Callable,
        on_cancel: Callable,
    ) -> None:
        """
        Show a warning dialog when the user toggles auto-accept ON.

        Explains that auto-accept will automatically approve all future
        file-change cards from the named agent. User can confirm or cancel.
        If canceled, the toggle snaps back to OFF. (Phase 5)

        Args:
            agent_name: Human-readable agent name for the dialog message.
            on_confirm: Callback to invoke if user clicks "Turn On".
            on_cancel: Callback to invoke if user clicks "Cancel".
        """
        import gi
        gi.require_version('Gtk', '4.0')
        from gi.repository import Gtk

        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.NONE,
            text=f"Enable Auto-Accept for {agent_name}?",
        )
        dialog.format_secondary_text(
            f"All future file-change cards from {agent_name} will be "
            f"automatically accepted without review. This cannot be undone "
            f"for cards already accepted."
        )
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Turn On", Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.CANCEL)

        def _on_response(dialog, response):
            if response == Gtk.ResponseType.OK:
                on_confirm()
            else:
                on_cancel()
            dialog.close()

        dialog.connect("response", _on_response)
        dialog.show()
```

## Rules

- Use the steelFramedCodeWriter prompt at `prompts/steelFramedCodeWriter.md`
- Do NOT modify any files other than `ui/window.py`
- Do NOT add tests — tests are in a later phase
- Do NOT modify `tests/test_low2_file_sandbox.py`
- Report: file locations with line numbers, grep evidence, test results
- Include a COMPLETENESS checklist with evidence for each edit

## Verify

1. `python3 -c "import ui.window"` — no import errors
2. `grep -n "set_show_auto_accept_warning\|_show_auto_accept_warning" ui/window.py` — both the wire call and the method present
3. `pytest tests/test_feed_store.py tests/test_feed_handler.py tests/test_feed_card.py tests/test_review_handler_feed_card.py tests/test_low12_13_feed.py -q --tb=short` — all pass (dialog is only shown on user click, not during tests)
