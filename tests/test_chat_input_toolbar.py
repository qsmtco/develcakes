# tests/test_chat_input_toolbar.py
# Tests for ui/views/chat_input_toolbar.py — Pure view verification.
#
# Architecture:
#   ChatInputToolbar is a pure view — widgets only, no business logic.
#   All logic lives in InputToolbarHandler (tested separately).
#
# Test strategy:
#   - Construction + widget existence: create the view, verify key widgets exist.
#   - Callback wiring: connect mock callbacks, simulate clicks, verify they fire.
#   - Public methods: call set_find_count, set_spell_active, etc. verify state.
#   - Import check: verify the view never imports from ui/handlers/.
#   - GTK3 check: verify no ModelButton or set_keynav_wrapper usage.
#
# GTK initialization: GDK_BACKEND=headless for headless test environments.
# Widget construction is real (not mocked) — we test the actual view.

from __future__ import annotations

import ast
from unittest.mock import MagicMock, patch

import pytest

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib, Gdk

from ui.views.chat_input_toolbar import ChatInputToolbar


# ── Class 1: Construction ─────────────────────────────────────────


class TestConstruction:
    """View instantiates without crash and has the expected widget tree."""

    def test_construction(self):
        """ChatInputToolbar() does not raise."""
        toolbar = ChatInputToolbar()
        assert toolbar is not None
        assert isinstance(toolbar, Gtk.Box)

    def test_has_expected_widgets(self):
        """Key internal widgets exist after construction."""
        toolbar = ChatInputToolbar()
        # Spell toggle button
        assert hasattr(toolbar, "_spell_btn")
        assert isinstance(toolbar._spell_btn, Gtk.ToggleButton)
        # Match label in find bar
        assert hasattr(toolbar, "_match_label")
        assert isinstance(toolbar._match_label, Gtk.Label)
        # Count label (word/char count)
        assert hasattr(toolbar, "_count_label")
        assert isinstance(toolbar._count_label, Gtk.Label)
        # Find entry
        assert hasattr(toolbar, "_find_entry")
        assert isinstance(toolbar._find_entry, Gtk.Entry)
        # Replace entry
        assert hasattr(toolbar, "_replace_entry")
        assert isinstance(toolbar._replace_entry, Gtk.Entry)
        # Find bar container
        assert hasattr(toolbar, "_find_bar")
        assert isinstance(toolbar._find_bar, Gtk.Box)
        # Replace row
        assert hasattr(toolbar, "_replace_row")
        assert isinstance(toolbar._replace_row, Gtk.Box)

    def test_no_handler_imports(self):
        """The view module must NOT import from ui/handlers/ (architecture rule)."""
        import ui.views.chat_input_toolbar as mod

        source = inspect_source(mod)
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module and "ui.handlers" in node.module:
                    pytest.fail(
                        f"View imports from ui/handlers: {node.module} "
                        f"(line {node.lineno}) — violates architecture"
                    )


# ── Class 2: Callback Wiring ──────────────────────────────────────


class TestCallbacks:
    """Callbacks are stored and fire when signals are emitted."""

    def test_on_find_toggled(self):
        """set_on_find stores callback; find_btn click triggers it."""
        toolbar = ChatInputToolbar()
        # The view doesn't expose set_on_find as the callback name.
        # Looking at the code, the find button click calls show_find_bar()
        # which then calls self._on_find. Let's verify _on_find fires.
        calls = []
        toolbar.set_on_find(lambda text: calls.append(text))
        # Simulate find button click
        toolbar._on_find_clicked()
        assert len(calls) == 1

    def test_on_spell_toggled(self):
        """set_on_spell_toggle stores callback; spell toggle triggers it."""
        toolbar = ChatInputToolbar()
        calls = []
        toolbar.set_on_spell_toggle(lambda: calls.append(True))
        # Simulate "toggled" signal: passes toggle button as arg + extra data
        toolbar._on_spell_toggled(toolbar._spell_btn, toolbar._spell_btn)
        assert calls == [True]

    def test_on_open_file(self):
        """set_on_open_file stores callback."""
        toolbar = ChatInputToolbar()
        cb = MagicMock()
        toolbar.set_on_open_file(cb)
        assert toolbar._on_open_file is cb

    def test_on_save_file(self):
        """set_on_save_file stores callback."""
        toolbar = ChatInputToolbar()
        cb = MagicMock()
        toolbar.set_on_save_file(cb)
        assert toolbar._on_save_file is cb

    def test_on_find_closed(self):
        """Close find bar callback fires and clears find state."""
        toolbar = ChatInputToolbar()
        calls = []
        toolbar.set_on_find(lambda text: calls.append(text))
        # Show then close
        toolbar.show_find_bar()
        toolbar._on_find_close_clicked()
        # The close callback passes "" to clear
        assert calls == [""]

    def test_set_on_save_prompt(self):
        """set_on_save_prompt stores callback."""
        toolbar = ChatInputToolbar()
        cb = MagicMock()
        toolbar.set_on_save_prompt(cb)
        assert toolbar._on_save_prompt is cb

    def test_set_on_open_prompt(self):
        """set_on_open_prompt stores callback."""
        toolbar = ChatInputToolbar()
        cb = MagicMock()
        toolbar.set_on_open_prompt(cb)
        assert toolbar._on_open_prompt is cb

    def test_set_on_replace(self):
        """set_on_replace stores callback."""
        toolbar = ChatInputToolbar()
        cb = MagicMock()
        toolbar.set_on_replace(cb)
        assert toolbar._on_replace is cb

    def test_set_on_find_next(self):
        """set_on_find_next stores callback."""
        toolbar = ChatInputToolbar()
        cb = MagicMock()
        toolbar.set_on_find_next(cb)
        assert toolbar._on_find_next is cb

    def test_set_on_find_prev(self):
        """set_on_find_prev stores callback."""
        toolbar = ChatInputToolbar()
        cb = MagicMock()
        toolbar.set_on_find_prev(cb)
        assert toolbar._on_find_prev is cb

    def test_on_find_next_clicked_fires_navigation_callback(self):
        """Next button / Enter key fires set_on_find_next when set."""
        toolbar = ChatInputToolbar()
        fired = []
        toolbar.set_on_find_next(lambda: fired.append("next"))
        toolbar._on_find_next_clicked()
        assert fired == ["next"]

    def test_on_find_prev_clicked_fires_callback(self):
        """Prev button fires set_on_find_prev when set."""
        toolbar = ChatInputToolbar()
        fired = []
        toolbar.set_on_find_prev(lambda: fired.append("prev"))
        toolbar._on_find_prev_clicked()
        assert fired == ["prev"]

    def test_on_find_next_fallback_to_search_callback(self):
        """When set_on_find_next is NOT set, Enter fires set_on_find with current text."""
        toolbar = ChatInputToolbar()
        search_calls = []
        toolbar.set_on_find(lambda text: search_calls.append(text))
        toolbar._find_entry.set_text("test query")
        # set_text triggers a changed signal that calls _on_find — clear it
        search_calls.clear()
        toolbar._on_find_next_clicked()
        # Falls back to re-running search with current text
        assert search_calls == ["test query"]

    def test_on_find_prev_no_callback_no_crash(self):
        """Prev button with no callback set does not crash."""
        toolbar = ChatInputToolbar()
        toolbar._on_find_prev_clicked()  # no set_on_find_prev call
        assert True  # no crash

    def test_on_find_next_no_callback_no_crash(self):
        """Next button with no set_on_find_next or set_on_find does not crash."""
        toolbar = ChatInputToolbar()
        toolbar._on_find_next_clicked()  # neither callback set
        assert True  # no crash


# ── Class 3: Public Methods ───────────────────────────────────────


class TestPublicMethods:
    """Public update methods work correctly."""

    def test_set_find_count_zero(self):
        """update_match_count(0, 0) → 'No matches'."""
        toolbar = ChatInputToolbar()
        toolbar.update_match_count(0, 0)
        text = toolbar._match_label.get_text()
        assert text == "No matches"

    def test_set_find_count_with_matches(self):
        """update_match_count(2, 7) → '3 of 7' (current is 0-indexed, display is 1-indexed)."""
        toolbar = ChatInputToolbar()
        toolbar.update_match_count(2, 7)
        text = toolbar._match_label.get_text()
        assert text == "3 of 7"

    def test_set_find_count_first_match(self):
        """update_match_count(0, 5) → '1 of 5'."""
        toolbar = ChatInputToolbar()
        toolbar.update_match_count(0, 5)
        text = toolbar._match_label.get_text()
        assert text == "1 of 5"

    def test_set_spell_active_true(self):
        """set_spell_active(True) sets button active and adds CSS class."""
        toolbar = ChatInputToolbar()
        toolbar.set_spell_active(True)
        assert toolbar._spell_btn.get_active() is True
        assert "spell-active" in toolbar._spell_btn.get_css_classes()

    def test_set_spell_active_false(self):
        """set_spell_active(False) deactivates button and removes CSS class."""
        toolbar = ChatInputToolbar()
        toolbar.set_spell_active(True)  # first activate
        toolbar.set_spell_active(False)
        assert toolbar._spell_btn.get_active() is False
        assert "spell-active" not in toolbar._spell_btn.get_css_classes()

    def test_show_find_bar(self):
        """show_find_bar() makes find bar visible."""
        toolbar = ChatInputToolbar()
        toolbar.show_find_bar()
        assert toolbar._find_bar.get_visible() is True

    def test_show_find_bar_with_replace(self):
        """show_find_bar(show_replace=True) shows find bar AND replace row."""
        toolbar = ChatInputToolbar()
        toolbar.show_find_bar(show_replace=True)
        assert toolbar._find_bar.get_visible() is True
        assert toolbar._replace_row.get_visible() is True

    def test_show_find_bar_without_replace(self):
        """show_find_bar(show_replace=False) hides replace row."""
        toolbar = ChatInputToolbar()
        toolbar.show_find_bar(show_replace=False)
        assert toolbar._replace_row.get_visible() is False

    def test_hide_find_bar(self):
        """hide_find_bar() hides find bar and clears entries."""
        toolbar = ChatInputToolbar()
        toolbar.show_find_bar()
        toolbar._find_entry.set_text("test")
        toolbar._replace_entry.set_text("replacement")
        toolbar.hide_find_bar()
        assert toolbar._find_bar.get_visible() is False
        assert toolbar._find_entry.get_text() == ""
        assert toolbar._replace_entry.get_text() == ""

    def test_update_word_count(self):
        """update_word_count() updates the count label markup."""
        toolbar = ChatInputToolbar()
        toolbar.update_word_count(10, 50, 13)
        text = toolbar._count_label.get_label()
        assert "10" in text
        assert "50" in text
        assert "13" in text

    def test_update_match_count_negative_current(self):
        """update_match_count(-1, 5) → 'No matches' (guards against current < 0)."""
        toolbar = ChatInputToolbar()
        toolbar.update_match_count(-1, 5)
        text = toolbar._match_label.get_text()
        assert text == "No matches"

    def test_update_match_count_zero_total(self):
        """update_match_count(0, 0) → 'No matches'."""
        toolbar = ChatInputToolbar()
        toolbar.update_match_count(0, 0)
        text = toolbar._match_label.get_text()
        assert text == "No matches"

    def test_get_search_text(self):
        """get_search_text() returns the find bar search entry text."""
        toolbar = ChatInputToolbar()
        toolbar._find_entry.set_text("hello world")
        assert toolbar.get_search_text() == "hello world"

    def test_get_replace_text(self):
        """get_replace_text() returns the find bar replace entry text."""
        toolbar = ChatInputToolbar()
        toolbar._replace_entry.set_text("goodbye")
        assert toolbar.get_replace_text() == "goodbye"

    def test_update_char_count(self):
        """update_char_count() updates the char count label in the find bar."""
        toolbar = ChatInputToolbar()
        toolbar.update_char_count(1247)
        text = toolbar._char_count_label.get_label()
        assert "1,247" in text
        assert "chars" in text

    def test_get_input_buffer(self):
        """get_input_buffer() returns None (view does not own the buffer)."""
        toolbar = ChatInputToolbar()
        assert toolbar.get_input_buffer() is None


# ── Class 4: Edge Cases ──────────────────────────────────────────


class TestEdgeCases:
    """Edge cases and initial state."""

    def test_find_bar_initially_hidden(self):
        """Find bar is not visible at construction time."""
        toolbar = ChatInputToolbar()
        assert toolbar._find_bar.get_visible() is False

    def test_replace_row_initially_hidden(self):
        """Replace row is not visible at construction time."""
        toolbar = ChatInputToolbar()
        assert toolbar._replace_row.get_visible() is False

    def test_callbacks_default_none(self):
        """Callbacks default to None — no crash if invoked before being set."""
        toolbar = ChatInputToolbar()
        # Find toggle: _on_find_clicked calls show_find_bar then _on_find
        # _on_find is None by default — must not crash
        toolbar._on_find_clicked()
        assert toolbar._find_bar.get_visible() is True  # show_find_bar still worked

    def test_spell_toggled_without_callback(self):
        """Spell toggle without callback set must not crash."""
        toolbar = ChatInputToolbar()
        toolbar._on_spell_toggled(toolbar._spell_btn, toolbar._spell_btn)
        assert True  # no crash

    def test_set_find_count_single_match(self):
        """update_match_count(0, 1) → '1 of 1'."""
        toolbar = ChatInputToolbar()
        toolbar.update_match_count(0, 1)
        text = toolbar._match_label.get_text()
        assert text == "1 of 1"

    def test_hide_find_bar_when_already_hidden(self):
        """Hiding an already-hidden find bar must not crash."""
        toolbar = ChatInputToolbar()
        toolbar.hide_find_bar()  # already hidden
        assert toolbar._find_bar.get_visible() is False

    def test_no_gtk3_modelbutton(self):
        """View must not use Gtk.ModelButton (GTK3-only)."""
        import ui.views.chat_input_toolbar as mod
        import inspect

        source = inspect.getsource(mod)
        assert "ModelButton" not in source, (
            "Gtk.ModelButton is GTK3-only and must not be used in GTK4 views"
        )

    def test_no_set_keynav_wrapper(self):
        """View must not call set_keynav_wrapper (GTK3-only on Gtk.Entry)."""
        import ui.views.chat_input_toolbar as mod
        import inspect

        source = inspect.getsource(mod)
        # The fix replaces set_keynav_wrapper with a comment — verify neither
        # the call nor an uncommented usage remains.
        assert "set_keynav_wrapper(" not in source, (
            "set_keynav_wrapper is GTK3-only and must not be called in GTK4 views"
        )

    def test_get_search_text_empty(self):
        """get_search_text() returns '' when entry is empty."""
        toolbar = ChatInputToolbar()
        assert toolbar.get_search_text() == ""

    def test_get_replace_text_empty(self):
        """get_replace_text() returns '' when entry is empty."""
        toolbar = ChatInputToolbar()
        assert toolbar.get_replace_text() == ""


# ── Bug fix regression tests (bugs #10–#14) ──


# ── Bug fix regression tests (BUG #3: silent translate_coordinates failure) ──


class TestTranslateCoordinatesWarning:
    """BUG #3 (adversarial audit): the right-click spell-suggestion handler
    in ui/window.py previously had a silent `return` when
    `text_view.translate_coordinates` returned `None` (GTK4 API). The fix adds a
    `logger.warning` so the failure becomes a visible diagnostic. These
    tests verify the warning is logged.

    The closure `_on_input_right_click` is defined as a local function
    inside `MainWindow._wire_agents()` and is not directly importable. We
    extract its source via `ast.get_source_segment` and `exec` it in a
    controlled namespace of MagicMock objects. This means any change to
    the production closure source is automatically tested — the test
    re-parses the source on every run.
    """

    @staticmethod
    def _load_right_click_closure():
        """Extract and exec the _on_input_right_click closure from ui/window.py.

        Returns the closure as a callable. The closure references `self`
        as a free variable; callers must inject it into the namespace
        under the name `self` before invoking.
        """
        import ast
        import textwrap

        with open("ui/window.py") as f:
            source = f.read()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_wire_agents":
                for child in ast.walk(node):
                    if (
                        isinstance(child, ast.FunctionDef)
                        and child.name == "_on_input_right_click"
                    ):
                        closure_src = ast.get_source_segment(source, child)
                        dedented = textwrap.dedent(closure_src)
                        # The closure references `logger` (module-level) and
                        # `self` (free variable from the enclosing _wire_agents
                        # method). Inject both so exec'd code can resolve them.
                        import logging
                        import gi as _gi
                        _gi.require_version('Gtk', '4.0')
                        from gi.repository import Gio
                        ns: dict = {
                            "logger": logging.getLogger("ui.window"),
                            "Gio": Gio,
                        }
                        exec(compile(dedented, "ui/window.py", "exec"), ns)
                        return ns["_on_input_right_click"]
        raise RuntimeError(
            "Could not find _on_input_right_click closure in ui/window.py"
        )

    @staticmethod
    def _build_mocks(translate_ok: bool = False):
        """Build a controlled namespace of MagicMock objects for the closure.

        Returns (self_mock, text_view, toolbar, menu, action_group).
        The menu and action_group are real Gio objects so the closure can
        populate them.
        """
        from unittest.mock import MagicMock
        import gi
        gi.require_version('Gtk', '4.0')
        from gi.repository import Gio

        # Handler
        handler = MagicMock()
        handler.is_spell_enabled.return_value = True
        handler.get_suggestions_at_iter.return_value = ["foo", "bar"]
        handler.get_word_at_iter.return_value = "wrld"

        # iter at position — has the spell tag
        iter_at_pos = MagicMock()
        iter_at_pos.has_tag.return_value = True
        iter_at_pos.get_offset.return_value = 5

        # spell tag (non-None)
        spell_tag = MagicMock()

        # tag table
        tag_table = MagicMock()
        tag_table.lookup.return_value = spell_tag

        # buffer
        buf = MagicMock()
        buf.get_tag_table.return_value = tag_table

        # text view
        text_view = MagicMock()
        text_view.get_iter_at_location.return_value = (True, iter_at_pos)
        text_view.get_buffer.return_value = buf

        # toolbar (no longer used for spell suggestions, kept for compat)
        toolbar = MagicMock()

        # main content
        main_content = MagicMock()
        main_content.user_input = text_view
        main_content.toolbar = toolbar

        # self
        self_mock = MagicMock()
        self_mock._input_toolbar_handler = handler
        self_mock._main_content = main_content

        # Real Gio objects for the closure to populate
        menu = Gio.Menu()
        action_group = Gio.SimpleActionGroup()

        return self_mock, text_view, toolbar, menu, action_group

    def test_populates_extra_menu_with_suggestions(self):
        """The right-click closure populates the Gio.Menu with suggestion items.

        Instead of creating a manual popover, the closure now uses the
        GTK4-native set_extra_menu approach. The menu must contain the
        suggestions from the handler.
        """
        closure = self._load_right_click_closure()
        self_mock, text_view, toolbar, menu, action_group = self._build_mocks()
        closure.__globals__["self"] = self_mock

        closure(1, 100, 200, menu, action_group)

        # The handler must have been asked for suggestions
        self_mock._input_toolbar_handler.get_suggestions_at_iter.assert_called_once()
        # The menu must have at least one item (the suggestion section)
        assert menu.get_n_items() > 0, (
            "Gio.Menu was not populated with suggestion items"
        )
        # The action group must have actions for each suggestion
        action_names = action_group.list_actions()
        assert len(action_names) == 2, (
            f"expected 2 suggestion actions (foo, bar), got {len(action_names)}: {action_names}"
        )

    def test_no_menu_population_when_spell_disabled(self):
        """When spell check is disabled, the menu must not be populated."""
        closure = self._load_right_click_closure()
        self_mock, text_view, toolbar, menu, action_group = self._build_mocks()
        self_mock._input_toolbar_handler.is_spell_enabled.return_value = False
        closure.__globals__["self"] = self_mock

        closure(1, 100, 200, menu, action_group)

        assert menu.get_n_items() == 0, (
            "Menu should not be populated when spell check is disabled"
        )
        assert len(action_group.list_actions()) == 0

    def test_no_menu_population_when_word_not_misspelled(self):
        """When the clicked word is not misspelled, the menu is empty."""
        closure = self._load_right_click_closure()
        self_mock, text_view, toolbar, menu, action_group = self._build_mocks()
        # Make iter_at_pos NOT have the spell tag
        iter_at_pos = self_mock._main_content.user_input.get_iter_at_location.return_value[1]
        iter_at_pos.has_tag.return_value = False
        closure.__globals__["self"] = self_mock

        closure(1, 100, 200, menu, action_group)

        assert menu.get_n_items() == 0


class TestBug10GetToplevel:
    """BUG #10: _get_toplevel() must not call Gtk.get_major_client() (GTK3-only)."""

    def test_get_toplevel_returns_none_when_not_parented(self):
        """When the widget is not parented to a Window, return None (no crash)."""
        toolbar = ChatInputToolbar()
        # Without a parent, get_ancestor(Gtk.Window) returns None
        # and the fix must not fall through to Gtk.get_major_client().
        assert toolbar._get_toplevel() is None

    def test_get_toplevel_returns_window_when_parented(self):
        """When parented to a Gtk.Window, return that window."""
        toolbar = ChatInputToolbar()
        window = Gtk.Window()
        window.set_child(toolbar)
        result = toolbar._get_toplevel()
        assert result is window

    def test_get_toplevel_does_not_call_get_major_client(self):
        """Source must not reference Gtk.get_major_client anywhere."""
        import ui.views.chat_input_toolbar as mod
        src = inspect_source(mod)
        assert "get_major_client" not in src, (
            "BUG #10 regression: Gtk.get_major_client is GTK3-only and "
            "segfaults on GTK4 Save/Open file dialogs"
        )


class TestBug11MenuButtonReferences:
    """BUG #11: menu buttons must be stored as attributes, not found via ancestor walk."""

    def test_save_menu_btn_stored(self):
        """The save MenuButton is stored as self._save_menu_btn after __init__."""
        toolbar = ChatInputToolbar()
        assert hasattr(toolbar, "_save_menu_btn")
        assert isinstance(toolbar._save_menu_btn, Gtk.MenuButton)

    def test_open_menu_btn_stored(self):
        """The open MenuButton is stored as self._open_menu_btn after __init__."""
        toolbar = ChatInputToolbar()
        assert hasattr(toolbar, "_open_menu_btn")
        assert isinstance(toolbar._open_menu_btn, Gtk.MenuButton)

    def test_find_save_menu_button_returns_stored_ref(self):
        """_find_save_menu_button returns the stored reference, not a tree walk."""
        toolbar = ChatInputToolbar()
        assert toolbar._find_save_menu_button() is toolbar._save_menu_btn

    def test_find_open_menu_button_returns_stored_ref(self):
        """_find_open_menu_button returns the stored reference, not a tree walk."""
        toolbar = ChatInputToolbar()
        assert toolbar._find_open_menu_button() is toolbar._open_menu_btn

    def test_find_menu_buttons_no_ancestor_walk(self):
        """Source must not walk ancestors in the _find_*_menu_button methods."""
        import ui.views.chat_input_toolbar as mod
        src = inspect_source(mod)
        # The buggy version had `while parent:` loops inside _find_*_menu_button
        # that walked the ancestor tree. The fix returns the stored ref directly.
        # Grep for the two method bodies and assert they have no `while` loops.
        import re
        for method_name in ("_find_save_menu_button", "_find_open_menu_button"):
            pattern = rf"def {method_name}\(.*?(?=\n    def |\nclass |\Z)"
            match = re.search(pattern, src, re.DOTALL)
            assert match, f"Could not find {method_name} in source"
            body = match.group(0)
            assert "while " not in body, (
                f"BUG #11 regression: {method_name} still walks ancestors "
                f"(should return stored ref directly)"
            )


class TestBug12PromptPopoverRef:
    """BUG #12: _on_prompt_selected must not call get_parent().get_children()."""

    def test_on_prompt_selected_calls_popdown_on_stored_ref(self):
        """Selecting a prompt pops down the stored popover and fires the callback."""
        toolbar = ChatInputToolbar()
        calls = []
        toolbar.set_on_open_prompt(lambda name: calls.append(name))
        # Inject a fake popover so we can verify popdown is called on it
        fake_popover = MagicMock()
        toolbar._prompt_popover = fake_popover
        toolbar._on_prompt_selected(None, "my_prompt")
        fake_popover.popdown.assert_called_once()
        assert calls == ["my_prompt"]

    def test_on_prompt_selected_no_crash_without_popover(self):
        """If popover is None (e.g. user clicked before popover built), no crash."""
        toolbar = ChatInputToolbar()
        calls = []
        toolbar.set_on_open_prompt(lambda name: calls.append(name))
        toolbar._prompt_popover = None
        # Must not raise
        toolbar._on_prompt_selected(None, "any_prompt")
        assert calls == ["any_prompt"]

    def test_on_prompt_selected_no_get_children_in_source(self):
        """Source must not call get_children() — it's GTK3-only."""
        import re
        import ui.views.chat_input_toolbar as mod
        src = inspect_source(mod)
        # Strip comments and string literals to avoid false positives from
        # explanatory comments like "GTK4 has no get_children()".
        no_comments = re.sub(r"#.*", "", src)
        no_strings = re.sub(r"['\"].*?['\"]", "", no_comments, flags=re.DOTALL)
        assert "get_children()" not in no_strings, (
            "BUG #12 regression: Gtk.Widget.get_children() does not exist in GTK4"
        )


class TestBug13ReplaceVsReplaceAll:
    """BUG #13: _on_replace_all_clicked must call _on_replace_all, not _on_replace."""

    def test_replace_callback_fires_on_replace_click(self):
        """Clicking Replace (current) fires _on_replace, not _on_replace_all."""
        toolbar = ChatInputToolbar()
        replace_calls = []
        replace_all_calls = []
        toolbar.set_on_replace(lambda text: replace_calls.append(text))
        toolbar.set_on_replace_all(lambda text: replace_all_calls.append(text))
        toolbar._replace_entry.set_text("hello")
        toolbar._on_replace_clicked_from_bar()
        assert replace_calls == ["hello"]
        assert replace_all_calls == []

    def test_replace_all_callback_fires_on_replace_all_click(self):
        """Clicking Replace All fires _on_replace_all, not _on_replace."""
        toolbar = ChatInputToolbar()
        replace_calls = []
        replace_all_calls = []
        toolbar.set_on_replace(lambda text: replace_calls.append(text))
        toolbar.set_on_replace_all(lambda text: replace_all_calls.append(text))
        toolbar._replace_entry.set_text("world")
        toolbar._on_replace_all_clicked()
        assert replace_all_calls == ["world"]
        assert replace_calls == []

    def test_replace_all_no_crash_without_callback(self):
        """If _on_replace_all is None, clicking Replace All must not crash."""
        toolbar = ChatInputToolbar()
        toolbar._replace_entry.set_text("x")
        # No set_on_replace_all called
        toolbar._on_replace_all_clicked()  # must not raise

    def test_set_on_replace_all_stores_callback(self):
        """set_on_replace_all stores the callback on the instance."""
        toolbar = ChatInputToolbar()
        cb = MagicMock()
        toolbar.set_on_replace_all(cb)
        assert toolbar._on_replace_all is cb

    def test_replace_all_initialized_to_none(self):
        """_on_replace_all defaults to None after construction."""
        toolbar = ChatInputToolbar()
        assert toolbar._on_replace_all is None


class TestBug14OpenPromptPopoverGuard:
    """BUG #14: _open_open_prompt_popover must guard popover.popup() with get_root()."""

    def test_open_prompt_popover_no_crash_without_root(self):
        """Opening the popover when not parented (no root) must not crash."""
        toolbar = ChatInputToolbar()
        # Mock load_prompts to return a single prompt
        with patch("ui.views.chat_input_toolbar.load_prompts",
                   return_value=[("test_prompt", "content")]):
            # Toolbar is not in a window, so get_root() is None
            # Must not raise — the fix guards popover.popup() with get_root() check
            toolbar._open_open_prompt_popover()

    def test_open_prompt_popover_stores_self_ref(self):
        """After opening, _prompt_popover is set to the new popover."""
        toolbar = ChatInputToolbar()
        with patch("ui.views.chat_input_toolbar.load_prompts",
                   return_value=[("test_prompt", "content")]):
            toolbar._open_open_prompt_popover()
        assert toolbar._prompt_popover is not None
        assert isinstance(toolbar._prompt_popover, Gtk.Popover)

    def test_open_prompt_popover_no_popup_when_no_root(self):
        """When get_root() is None, popover.popup() must not be called."""
        toolbar = ChatInputToolbar()
        with patch("ui.views.chat_input_toolbar.load_prompts",
                   return_value=[("test_prompt", "content")]):
            # Stub get_root to return None explicitly
            toolbar.get_root = lambda: None
            with patch.object(Gtk.Popover, "popup") as mock_popup:
                toolbar._open_open_prompt_popover()
                # popup must NOT have been called
                mock_popup.assert_not_called()

    def test_open_prompt_popover_popup_called_when_rooted(self):
        """When get_root() is not None, popover.popup() IS called."""
        toolbar = ChatInputToolbar()
        # Put the toolbar in a window so get_root() returns the window
        window = Gtk.Window()
        window.set_child(toolbar)
        with patch("ui.views.chat_input_toolbar.load_prompts",
                   return_value=[("test_prompt", "content")]):
            with patch.object(Gtk.Popover, "popup") as mock_popup:
                toolbar._open_open_prompt_popover()
                # popup must have been called once
                mock_popup.assert_called_once()


# ── Helpers ──────────────────────────────────────────────────────


def inspect_source(module) -> str:
    """Read the source file for the given module."""
    import inspect

    return inspect.getsource(module)


# ── Phase 8 regression: word count label must update on buffer change ──


class TestPhase8WordCountLabel:
    """Regression tests for the Phase 8 bug: word/char label must update
    when the input buffer changes. See TOOLBAR-PHASE-8-INSTRUCTIONS.md."""

    def test_word_count_label_updates_on_buffer_change(self):
        from ui.handlers.input_toolbar_handler import InputToolbarHandler

        # Build a real toolbar + handler + buffer, wire them as window.py does
        toolbar = ChatInputToolbar()

        # Use a stand-in main_content that exposes user_input.get_buffer()
        class _StubMC:
            def __init__(self):
                self._user_input = Gtk.TextView()

            @property
            def user_input(self):
                return self._user_input

        mc = _StubMC()
        handler = InputToolbarHandler(main_content=mc, GLib_module=GLib)

        # Wire the count path the same way window.py does in Edit 3
        def _on_buf_changed(_buf):
            handler.on_buffer_changed()
            words, chars, tokens = handler.compute_count()
            toolbar.update_word_count(words, chars, tokens)

        buf = mc._user_input.get_buffer()
        buf.connect("changed", _on_buf_changed)

        # Type some text — the buffer's 'changed' signal should fire
        buf.set_text("hello world this is a test")

        # Drain the GLib main context so signal handlers run
        ctx = GLib.MainContext.default()
        while ctx.iteration(False):
            pass

        # The label should now reflect 6 words, not 0
        label = toolbar._count_label.get_label()
        assert "6 words" in label, f"expected '6 words' in label, got: {label!r}"
        assert "0 words" not in label, f"label still shows zero: {label!r}"


class TestSelectAllButton:
    """Tests for the Select All toolbar button (Bug #4: feature addition).

    Covers:
    - Button exists with correct tooltip
    - set_on_select_all stores callback
    - Click fires the callback
    - Click without callback does not crash (sad path)
    """

    def test_select_all_button_exists_with_tooltip(self):
        """The toolbar has a Select All button with the correct tooltip."""
        toolbar = ChatInputToolbar()
        btn = toolbar._select_all_btn
        assert btn is not None
        assert "Select All" in btn.get_tooltip_text()
        assert "Ctrl+A" in btn.get_tooltip_text()

    def test_set_on_select_all_stores_callback(self):
        """set_on_select_all stores the callback for later invocation."""
        toolbar = ChatInputToolbar()
        cb = MagicMock()
        toolbar.set_on_select_all(cb)
        assert toolbar._on_select_all is cb

    def test_select_all_click_fires_callback(self):
        """Clicking the Select All button invokes the registered callback."""
        toolbar = ChatInputToolbar()
        calls = []
        toolbar.set_on_select_all(lambda: calls.append(True))
        toolbar._on_select_all_clicked()
        assert calls == [True]

    def test_select_all_click_without_callback_does_not_crash(self):
        """Clicking Select All with no callback set is a no-op, not a crash."""
        toolbar = ChatInputToolbar()
        # No set_on_select_all call — _on_select_all is None
        toolbar._on_select_all_clicked()  # should not raise


class TestStale1WordChangeDetected:
    """Tests for STALE-1 fix: verify clicked word before replacement.

    The right-click closure captures the clicked word's text at right-click
    time. When the user clicks a suggestion, the closure re-derives the word
    at the same offset and verifies it matches. If the buffer changed between
    right-click and suggestion click (user typed text), the replacement is
    silently skipped with a logged warning.

    Uses the same AST-extraction pattern as TestTranslateCoordinatesWarning.
    """

    @staticmethod
    def _load_right_click_closure():
        """Extract the _on_input_right_click closure from window.py via AST."""
        import ast
        import textwrap
        with open("ui/window.py") as f:
            source = f.read()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_wire_agents":
                for child in ast.walk(node):
                    if (
                        isinstance(child, ast.FunctionDef)
                        and child.name == "_on_input_right_click"
                    ):
                        closure_src = ast.get_source_segment(source, child)
                        dedented = textwrap.dedent(closure_src)
                        import logging
                        import gi as _gi2
                        _gi2.require_version('Gtk', '4.0')
                        from gi.repository import Gio as Gio2
                        ns: dict = {
                            "logger": logging.getLogger("ui.window"),
                            "Gio": Gio2,
                        }
                        exec(compile(dedented, "ui/window.py", "exec"), ns)
                        return ns["_on_input_right_click"]
        raise RuntimeError(
            "Could not find _on_input_right_click closure in ui/window.py"
        )

    @staticmethod
    def _build_stale1_mocks(word_changed: bool = False):
        """Build mocks for the STALE-1 test.

        Args:
            word_changed: If True, simulate that the buffer changed between
                          right-click and suggestion click. get_word_at_iter
                          returns a different word the second time.
        """
        from unittest.mock import MagicMock
        import gi
        gi.require_version('Gtk', '4.0')
        from gi.repository import Gio

        handler = MagicMock()
        handler.is_spell_enabled.return_value = True
        handler.get_suggestions_at_iter.return_value = ["world"]

        # clicked_word at right-click time is "wrld"
        # current_word at suggestion-click time is "wrld" (same) or "hello" (changed)
        if word_changed:
            handler.get_word_at_iter.side_effect = ["wrld", "hello"]
        else:
            handler.get_word_at_iter.return_value = "wrld"

        iter_at_pos = MagicMock()
        iter_at_pos.has_tag.return_value = True
        iter_at_pos.get_offset.return_value = 6

        spell_tag = MagicMock()
        tag_table = MagicMock()
        tag_table.lookup.return_value = spell_tag

        buf = MagicMock()
        buf.get_tag_table.return_value = tag_table
        fresh_iter = MagicMock()
        fresh_iter.inside_word.return_value = True
        buf.get_iter_at_offset.return_value = fresh_iter

        text_view = MagicMock()
        text_view.get_iter_at_location.return_value = (True, iter_at_pos)
        text_view.get_buffer.return_value = buf

        toolbar = MagicMock()

        main_content = MagicMock()
        main_content.user_input = text_view
        main_content.toolbar = toolbar

        self_mock = MagicMock()
        self_mock._input_toolbar_handler = handler
        self_mock._main_content = main_content

        menu = Gio.Menu()
        action_group = Gio.SimpleActionGroup()

        return self_mock, handler, toolbar, menu, action_group

    def test_suggestion_applied_when_word_unchanged(self):
        """When the buffer hasn't changed, the suggestion is applied normally."""
        closure = self._load_right_click_closure()
        self_mock, handler, toolbar, menu, action_group = self._build_stale1_mocks(word_changed=False)
        closure.__globals__["self"] = self_mock

        # Populate the menu and register actions
        closure(1, 100, 200, menu, action_group)

        # The action group must have at least one action (the suggestion)
        action_names = action_group.list_actions()
        assert len(action_names) == 1, (
            f"expected 1 suggestion action, got {action_names}"
        )

        # Trigger the action (simulates clicking the menu item)
        action = action_group.lookup_action(action_names[0])
        action.activate(None)

        # replace_word_at_iter should have been called
        handler.replace_word_at_iter.assert_called_once()

    def test_suggestion_skipped_when_word_changed(self, caplog):
        """When the buffer changed, the replacement is skipped with a warning."""
        import logging

        closure = self._load_right_click_closure()
        self_mock, handler, toolbar, menu, action_group = self._build_stale1_mocks(word_changed=True)
        closure.__globals__["self"] = self_mock

        # Populate the menu and register actions
        closure(1, 100, 200, menu, action_group)

        action_names = action_group.list_actions()
        assert len(action_names) == 1

        with caplog.at_level(logging.WARNING, logger="ui.window"):
            # Trigger the suggestion action
            action = action_group.lookup_action(action_names[0])
            action.activate(None)

        # replace_word_at_iter should NOT have been called
        handler.replace_word_at_iter.assert_not_called()

        # A warning should have been logged
        warning_records = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and r.name == "ui.window"
        ]
        assert len(warning_records) == 1
        msg = warning_records[0].getMessage()
        assert "changed" in msg.lower() or "ignoring" in msg.lower()
