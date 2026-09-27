# tests/test_chat_heading.py
# Tests for Bug #1 + #5: _build_heading_segment renders inline markdown
# and applies two separate CSS classes via make_safe_label(css_classes=...).

import pytest


def _gtk_skip():
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk  # noqa: F401
    except (ImportError, ValueError):
        return True
    return False


class TestHeadingSegmentMarkdown:
    """Bug #1: heading content must run through format_markdown."""

    def test_plain_heading(self):
        if _gtk_skip():
            pytest.skip("GTK not available in test environment")
        from ui.views.event_cards import _build_heading_segment
        seg = {"level": 2, "content": "plain"}
        label = _build_heading_segment(seg)
        assert label.get_label() == "plain"

    def test_bold_heading(self):
        if _gtk_skip():
            pytest.skip("GTK not available in test environment")
        from ui.views.event_cards import _build_heading_segment
        seg = {"level": 3, "content": "**Important** conference"}
        label = _build_heading_segment(seg)
        markup = label.get_label()
        assert "<b>Important</b>" in markup
        assert "**" not in markup

    def test_italic_heading(self):
        if _gtk_skip():
            pytest.skip("GTK not available in test environment")
        from ui.views.event_cards import _build_heading_segment
        seg = {"level": 2, "content": "and *italic* here"}
        label = _build_heading_segment(seg)
        markup = label.get_label()
        assert "<i>italic</i>" in markup

    def test_code_span_heading(self):
        if _gtk_skip():
            pytest.skip("GTK not available in test environment")
        from ui.views.event_cards import _build_heading_segment
        seg = {"level": 2, "content": "using `var` here"}
        label = _build_heading_segment(seg)
        markup = label.get_label()
        assert "<tt>var</tt>" in markup

    def test_link_heading(self):
        if _gtk_skip():
            pytest.skip("GTK not available in test environment")
        from ui.views.event_cards import _build_heading_segment
        seg = {"level": 2, "content": "[click](https://example.com)"}
        label = _build_heading_segment(seg)
        markup = label.get_label()
        # Markdown link syntax is consumed and rendered as underlined text.
        # Pango rejects <a href> ("Unknown tag 'a'"), so links are deliberately
        # non-clickable — see utils/markdown.py:253 (commit 676eb1f) and the
        # <u> assertions in tests/test_markdown.py.
        assert "<u>click</u>" in markup
        assert "[click](" not in markup
        assert "href=" not in markup

    def test_javascript_link_blocked(self):
        """HIGH-6: javascript: links in headings must be blocked by activate-link."""
        if _gtk_skip():
            pytest.skip("GTK not available in test environment")
        from ui.views.event_cards import _build_heading_segment
        seg = {"level": 2, "content": "[click](javascript:alert(1))"}
        label = _build_heading_segment(seg)
        retval = label.emit("activate-link", "javascript:alert(1)")
        assert retval is True, "javascript: link was not blocked"

    def test_safe_link_allowed(self):
        """HIGH-6: https links in headings must NOT be blocked."""
        if _gtk_skip():
            pytest.skip("GTK not available in test environment")
        from ui.views.event_cards import _build_heading_segment
        seg = {"level": 2, "content": "[click](https://example.com)"}
        label = _build_heading_segment(seg)
        retval = label.emit("activate-link", "https://example.com")
        assert retval is False

    def test_empty_content_returns_box(self):
        if _gtk_skip():
            pytest.skip("GTK not available in test environment")
        from ui.views.event_cards import _build_heading_segment
        import gi
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk
        seg = {"level": 2, "content": ""}
        widget = _build_heading_segment(seg)
        assert not isinstance(widget, Gtk.Label), "empty heading should return spacer Box"

    def test_whitespace_only_returns_box(self):
        if _gtk_skip():
            pytest.skip("GTK not available in test environment")
        from ui.views.event_cards import _build_heading_segment
        import gi
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk
        seg = {"level": 2, "content": "   "}
        widget = _build_heading_segment(seg)
        assert not isinstance(widget, Gtk.Label)

    def test_ampersand_escaped(self):
        if _gtk_skip():
            pytest.skip("GTK not available in test environment")
        from ui.views.event_cards import _build_heading_segment
        seg = {"level": 2, "content": "a & b"}
        label = _build_heading_segment(seg)
        markup = label.get_label()
        assert "&amp;" in markup


class TestHeadingSegmentCssClasses:
    """Bug #5: heading must have two SEPARATE CSS classes, not one compound."""

    def test_level2_two_classes(self):
        if _gtk_skip():
            pytest.skip("GTK not available in test environment")
        from ui.views.event_cards import _build_heading_segment
        seg = {"level": 2, "content": "test"}
        label = _build_heading_segment(seg)
        classes = label.get_css_classes()
        assert "chat-heading" in classes, f"missing chat-heading: {classes}"
        assert "chat-heading-2" in classes, f"missing chat-heading-2: {classes}"
        assert "chat-heading chat-heading-2" not in classes, (
            f"compound class bug: {classes}"
        )

    def test_level1_two_classes(self):
        if _gtk_skip():
            pytest.skip("GTK not available in test environment")
        from ui.views.event_cards import _build_heading_segment
        seg = {"level": 1, "content": "test"}
        label = _build_heading_segment(seg)
        classes = label.get_css_classes()
        assert "chat-heading" in classes
        assert "chat-heading-1" in classes

    def test_level_capped_at_4(self):
        if _gtk_skip():
            pytest.skip("GTK not available in test environment")
        from ui.views.event_cards import _build_heading_segment
        seg = {"level": 99, "content": "x"}
        label = _build_heading_segment(seg)
        classes = label.get_css_classes()
        assert "chat-heading-4" in classes, f"level not capped: {classes}"
        assert "chat-heading-99" not in classes


class TestHeadingSegmentLevelGuard:
    """BUG #1 (audit): level field must be guarded against non-int types."""

    def test_level_none_falls_back_to_default(self):
        if _gtk_skip():
            pytest.skip("GTK not available in test environment")
        from ui.views.event_cards import _build_heading_segment
        w = _build_heading_segment({"level": None, "content": "hi"})
        assert "chat-heading" in w.get_css_classes()

    def test_level_string_falls_back_to_default(self):
        if _gtk_skip():
            pytest.skip("GTK not available in test environment")
        from ui.views.event_cards import _build_heading_segment
        w = _build_heading_segment({"level": "high", "content": "hi"})
        assert "chat-heading" in w.get_css_classes()

    def test_level_negative_clamped_to_1(self):
        if _gtk_skip():
            pytest.skip("GTK not available in test environment")
        from ui.views.event_cards import _build_heading_segment
        w = _build_heading_segment({"level": -1, "content": "hi"})
        classes = w.get_css_classes()
        assert "chat-heading-1" in classes, f"negative not clamped: {classes}"

    def test_level_zero_clamped_to_1(self):
        if _gtk_skip():
            pytest.skip("GTK not available in test environment")
        from ui.views.event_cards import _build_heading_segment
        w = _build_heading_segment({"level": 0, "content": "hi"})
        classes = w.get_css_classes()
        assert "chat-heading-1" in classes, f"zero not clamped: {classes}"

    def test_level_float_truncated(self):
        if _gtk_skip():
            pytest.skip("GTK not available in test environment")
        from ui.views.event_cards import _build_heading_segment
        w = _build_heading_segment({"level": 2.7, "content": "hi"})
        classes = w.get_css_classes()
        assert "chat-heading-2" in classes, f"float not truncated: {classes}"
