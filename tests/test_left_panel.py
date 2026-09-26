import os
import tempfile
from unittest.mock import MagicMock, patch

# GTK4 must be importable for tests
import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, Gio, GLib, Gdk

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ui.views.left_panel import LeftPanel


class TestPromptRowRightClick:
    """Tests for the right-click copy menu on prompt rows."""

    def test_prompt_row_has_filepath_and_content_attrs(self):
        """Verify _build_prompt_row sets row._filepath and row._prompt_content from the prompt dict."""
        panel = LeftPanel()
        panel._prompts_handler = MagicMock()

        prompt = {
            'filepath': '/abs/path/to/prompt.md',
            'name': 'test-prompt',
            'content': 'This is the prompt content.',
            'is_favorite': False,
            'lines': 10,
            'size': 100,
            'last_used_str': 'today'
        }

        row = panel._build_prompt_row(prompt)

        assert hasattr(row, '_filepath'), "row should have _filepath attribute"
        assert hasattr(row, '_prompt_content'), "row should have _prompt_content attribute"
        assert row._filepath == '/abs/path/to/prompt.md'
        assert row._prompt_content == 'This is the prompt content.'

    def test_copy_path_calls_clipboard_with_filepath(self):
        """Patch Gdk.Display.get_default, call _on_copy_prompt_path, verify clipboard.set was called with filepath."""
        panel = LeftPanel()
        panel._prompt_copy_status_label = Gtk.Label()
        panel._prompt_copy_status_timeout_id = None

        row = Gtk.ListBoxRow()
        row._filepath = '/abs/path/to/prompt.md'

        with patch('gi.repository.Gdk.Display.get_default') as mock_display:
            mock_clipboard = MagicMock()
            mock_display.return_value.get_clipboard.return_value = mock_clipboard

            panel._on_copy_prompt_path(row)

            mock_clipboard.set.assert_called_once_with('/abs/path/to/prompt.md')

    def test_copy_prompt_calls_clipboard_with_content(self):
        """Patch Gdk.Display.get_default, call _on_copy_prompt_content, verify clipboard.set was called with content."""
        panel = LeftPanel()
        panel._prompt_copy_status_label = Gtk.Label()
        panel._prompt_copy_status_timeout_id = None

        row = Gtk.ListBoxRow()
        row._prompt_content = 'This is the prompt content.'

        with patch('gi.repository.Gdk.Display.get_default') as mock_display:
            mock_clipboard = MagicMock()
            mock_display.return_value.get_clipboard.return_value = mock_clipboard

            panel._on_copy_prompt_content(row)

            mock_clipboard.set.assert_called_once_with('This is the prompt content.')

    def test_copy_path_skips_when_filepath_missing(self):
        """Row with no _filepath attr -> no clipboard call, no exception."""
        panel = LeftPanel()
        panel._prompt_copy_status_label = Gtk.Label()
        panel._prompt_copy_status_timeout_id = None

        row = Gtk.ListBoxRow()
        # No _filepath attribute

        with patch('gi.repository.Gdk.Display.get_default') as mock_display:
            mock_clipboard = MagicMock()
            mock_display.return_value.get_clipboard.return_value = mock_clipboard

            panel._on_copy_prompt_path(row)

            mock_clipboard.set.assert_not_called()

    def test_copy_prompt_skips_when_content_missing(self):
        """Row with no _prompt_content attr -> no clipboard call, no exception."""
        panel = LeftPanel()
        panel._prompt_copy_status_label = Gtk.Label()
        panel._prompt_copy_status_timeout_id = None

        row = Gtk.ListBoxRow()
        # No _prompt_content attribute

        with patch('gi.repository.Gdk.Display.get_default') as mock_display:
            mock_clipboard = MagicMock()
            mock_display.return_value.get_clipboard.return_value = mock_clipboard

            panel._on_copy_prompt_content(row)

            mock_clipboard.set.assert_not_called()

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

    def test_right_click_handler_ignores_multipress(self):
        """Call _on_prompt_row_right_click with n_press=2, verify no popover is created."""
        panel = LeftPanel()
        panel._prompt_copy_status_label = Gtk.Label()
        panel._prompt_copy_status_timeout_id = None

        row = Gtk.ListBoxRow()
        row._filepath = '/abs/path/to/prompt.md'

        with patch('gi.repository.Gtk.Popover') as mock_popover_class:
            mock_popover_class.return_value = MagicMock()

            panel._on_prompt_row_right_click(None, 2, 0, 0, row)

            mock_popover_class.assert_not_called()

    def test_menu_row_dispatch_uses_action_key_not_label(self):
        """
        Regression: label-text dispatch (Bug #2 in Tier-3 follow-ups) broke under any
        translation. Fix: each menu row carries an _action attribute set at row build
        time, and dispatch reads _action — not the child label text.
        """
        panel = LeftPanel()
        panel._prompt_copy_status_label = Gtk.Label()
        panel._prompt_copy_status_timeout_id = None

        # Build a fake source row with the required attrs
        source_row = type("FakeRow", (), {"_filepath": "/tmp/x.md", "_prompt_content": "x"})()

        # Build the two menu rows the same way the production code does
        copy_path_row = Gtk.ListBoxRow()
        copy_path_row.set_activatable(True)
        copy_path_row.set_selectable(False)
        copy_path_row._action = "copy_path"
        copy_path_label = Gtk.Label(label="Copy path", xalign=0)
        copy_path_row.set_child(copy_path_label)

        copy_content_row = Gtk.ListBoxRow()
        copy_content_row.set_activatable(True)
        copy_content_row.set_selectable(False)
        copy_content_row._action = "copy_prompt"
        copy_content_label = Gtk.Label(label="Copy prompt", xalign=0)
        copy_content_row.set_child(copy_content_label)

        # Verify action keys are set
        assert copy_path_row._action == "copy_path"
        assert copy_content_row._action == "copy_prompt"

        mock_popover = MagicMock()

        # Test dispatch via action key: copy_path
        with patch.object(panel, '_on_copy_prompt_path') as mock_copy_path:
            panel._on_prompt_menu_row_activated(None, copy_path_row, mock_popover, source_row)
            mock_copy_path.assert_called_once_with(source_row)

        # Test dispatch via action key: copy_prompt
        with patch.object(panel, '_on_copy_prompt_content') as mock_copy_content:
            panel._on_prompt_menu_row_activated(None, copy_content_row, mock_popover, source_row)
            mock_copy_content.assert_called_once_with(source_row)

        # Now mutate the child label to a non-English string — dispatch must still work
        copy_path_row.get_child().set_text("Copier le chemin")
        copy_content_row.get_child().set_text("Copier l'invite")

        with patch.object(panel, '_on_copy_prompt_path') as mock_copy_path:
            panel._on_prompt_menu_row_activated(None, copy_path_row, mock_popover, source_row)
            mock_copy_path.assert_called_once_with(source_row)

        with patch.object(panel, '_on_copy_prompt_content') as mock_copy_content:
            panel._on_prompt_menu_row_activated(None, copy_content_row, mock_popover, source_row)
            mock_copy_content.assert_called_once_with(source_row)

    def test_popover_closed_handler_unparents(self):
        """
        Regression: popover leak on ESC / click-outside dismiss (Bug #1 in Tier-3
        follow-ups). Fix: _on_prompt_row_right_click connects the "closed" signal
        to a handler that calls popover.unparent().

        This test calls _on_prompt_row_right_click (the production code path) and
        verifies the resulting popover has a "closed" signal handler that unparents it.
        Regression-proof: FAILS if the popover.connect("closed", ...) line is removed.
        """
        panel = LeftPanel()
        panel._prompt_copy_status_label = Gtk.Label()
        panel._prompt_copy_status_timeout_id = None

        row = Gtk.ListBoxRow()
        row._filepath = '/abs/path/to/prompt.md'

        # Capture the popover that _on_prompt_row_right_click creates
        created_popovers = []
        original_popup = Gtk.Popover.popup

        def capture_popup(self_popover):
            created_popovers.append(self_popover)
            # Don't actually popup (no display in test env), just record it
            # Call the parent method is not needed; we just need the reference

        with patch.object(Gtk.Popover, 'popup', capture_popup):
            panel._on_prompt_row_right_click(None, 1, 0, 0, row)

        assert len(created_popovers) == 1, (
            "_on_prompt_row_right_click should create exactly one popover"
        )
        popover = created_popovers[0]

        # Verify the popover is parented to the row
        assert popover.get_parent() is row, "popover should be parented to row"

        # Fire the "closed" signal — if the handler is connected, this unparents the popover
        popover.emit("closed")

        # After the closed signal fires, the popover should be unparented
        assert popover.get_parent() is None, (
            "After 'closed' signal fires, popover should be unparented. "
            "If this fails, the closed signal handler is not connected in "
            "_on_prompt_row_right_click."
        )

    def test_prompt_row_has_right_click_gesture_attached(self):
        """Verify _build_prompt_row actually attaches a right-click GestureClick controller
        to the row. This tests the USER-FACING wiring (Edit 5), not just the helper.

        Per steelFramedCodeWriter Rule 4 + adversarialDebugger §11: a test that only calls
        the handler would pass even if the gesture were never attached, hiding a real
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


class TestAgentRowSourceTag:
    """Retro-audit fix round, Part B (rename slice R4): the Agents-panel
    agent-row tag line shows the emitting app's source as 'DevelCakes' for
    special (local) agents. Located via the stable agent-tag-label CSS
    class, never by position. Falsifier: revert source_tag to 'Crabcakes'
    (left_panel.py) → the identity assert fails."""

    def test_special_agent_tag_says_develcakes(self):
        panel = LeftPanel()
        panel._agent_list_handler = None  # exercise the built-in initials path

        row = panel._build_agent_row("special:coder", "Coder", in_project=False,
                                     session_count=1)

        from gi.repository import Gtk as _Gtk
        labels = []

        def _walk(w):
            labels.append(w)
            child = w.get_first_child()
            while child is not None:
                _walk(child)
                child = child.get_next_sibling()

        _walk(row)
        tag_labels = [w for w in labels
                      if isinstance(w, _Gtk.Label)
                      and w.has_css_class("agent-tag-label")]
        assert len(tag_labels) == 1, (
            "exactly one agent-tag-label per agent row (locator discipline)"
        )
        text = tag_labels[0].get_text()
        assert "DevelCakes" in text, (
            f"agent-row source tag must say 'DevelCakes', got {text!r} — "
            "the rename was reverted or the label was restructured"
        )
        assert "Crabcakes" not in text
        assert text.endswith("1 Session")
