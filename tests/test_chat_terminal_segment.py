# tests/test_chat_terminal_segment.py
# Tests for Bug #3 + #8: _build_terminal_segment renders inline markdown
# and blocks javascript: links via make_safe_label per-line.

import pytest


def _gtk_skip():
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk  # noqa: F401
    except (ImportError, ValueError):
        return True
    return False


class TestTerminalSegment:

    def test_bold_in_terminal_line(self):
        """Terminal content with **bold** must render as <b>bold</b>, not literal **."""
        if _gtk_skip():
            pytest.skip("GTK not available in test environment")
        from ui.views.event_cards import _build_terminal_segment
        widget = _build_terminal_segment({"content": "error with **bold** message"})
        # Walk widget tree to find the content label (nested in row > make_safe_label)
        def find_label_with(w, needle):
            if hasattr(w, "get_label"):
                if needle in w.get_label():
                    return True
            child = w.get_first_child()
            while child:
                if find_label_with(child, needle):
                    return True
                child = child.get_next_sibling()
            return False
        assert find_label_with(widget, "<b>bold</b>"), "bold not rendered in terminal line"

    def test_https_link_in_terminal(self):
        """Terminal content with [docs](https://...) must render the link text.

        Current contract (utils/markdown.py:253, commit 676eb1f): markdown
        links render as <u>text</u> with NO href — Pango rejects the <a> tag
        ("Unknown tag 'a'"), so links are underlined but not clickable.
        """
        if _gtk_skip():
            pytest.skip("GTK not available in test environment")
        from ui.views.event_cards import _build_terminal_segment
        widget = _build_terminal_segment({"content": "see [docs](https://example.com)"})
        def find_label_with(w, needle):
            if hasattr(w, "get_label"):
                if needle in w.get_label():
                    return True
            child = w.get_first_child()
            while child:
                if find_label_with(child, needle):
                    return True
                child = child.get_next_sibling()
            return False
        assert find_label_with(widget, "<u>docs</u>")
        # Non-clickable by design — no anchor markup anywhere in the segment.
        assert not find_label_with(widget, "href=")

    def test_javascript_link_blocked(self):
        """HIGH-6: javascript: links in terminal must never become clickable.

        Current contract (utils/markdown.py:257-260): the link renders as
        underlined text behind the red HIGH-6 warning prefix, with no href —
        so it is not an anchor at all. The activate-link guard stays as the
        backstop for any anchor that does carry an href.
        """
        if _gtk_skip():
            pytest.skip("GTK not available in test environment")
        from ui.views.event_cards import _build_terminal_segment
        widget = _build_terminal_segment({"content": "see [x](javascript:alert(1))"})
        # Find the label carrying the needle (returns the widget, not a bool).
        def find_label_with(w, needle):
            if hasattr(w, "get_label") and needle in w.get_label():
                return w
            child = w.get_first_child()
            while child:
                found = find_label_with(child, needle)
                if found is not None:
                    return found
                child = child.get_next_sibling()
            return None
        # No clickable anchor is emitted for a non-allowlisted scheme.
        assert find_label_with(widget, "href=") is None
        # The HIGH-6 warning prefix (U+26A0) marks it as non-allowlisted.
        js_label = find_label_with(widget, "\u26a0")
        assert js_label is not None, "javascript: link missing HIGH-6 warning prefix"
        assert "<u>x</u>" in js_label.get_label()
        # Backstop: the activate-link handler blocks the scheme outright.
        assert js_label.emit("activate-link", "javascript:alert(1)") is True

    def test_plain_text_unchanged(self):
        """Regression: plain terminal text must render without Pango conversion."""
        if _gtk_skip():
            pytest.skip("GTK not available in test environment")
        from ui.views.event_cards import _build_terminal_segment
        widget = _build_terminal_segment({"content": "plain text"})
        def find_label_with(w, needle):
            if hasattr(w, "get_label"):
                # Strip the Pango wrapper tags to check the visible text
                import re
                visible = re.sub(r'<[^>]+>', '', w.get_label())
                if needle in visible:
                    return True
            child = w.get_first_child()
            while child:
                if find_label_with(child, needle):
                    return True
                child = child.get_next_sibling()
            return False
        assert find_label_with(widget, "plain text")

    def test_empty_content_returns_box(self):
        """BUG #7: empty terminal content must return an empty spacer, not a full block."""
        if _gtk_skip():
            pytest.skip("GTK not available in test environment")
        from ui.views.event_cards import _build_terminal_segment
        import gi
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk
        widget = _build_terminal_segment({"content": ""})
        # The empty spacer is a plain Gtk.Box with no children.
        # A full terminal block would have the terminal-block CSS class.
        assert "terminal-block" not in widget.get_css_classes(), (
            "empty terminal should return spacer, not full block"
        )