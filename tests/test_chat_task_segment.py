# tests/test_chat_task_segment.py
# Tests for Bug #2: _build_task_segment renders inline markdown + HIGH-6 guard.

import pytest


def _gtk_skip():
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk  # noqa: F401
    except (ImportError, ValueError):
        return True
    return False


class TestTaskSegmentMarkdown:
    """Bug #2: task content must run through format_markdown."""

    def test_bold_task(self):
        if _gtk_skip():
            pytest.skip("GTK not available in test environment")
        from ui.views.event_cards import _build_task_segment
        seg = {"content": "[x] **bold** task"}
        label = _build_task_segment(seg)
        markup = label.get_label()
        assert "<b>bold</b>" in markup
        assert "**" not in markup

    def test_unchecked_box(self):
        if _gtk_skip():
            pytest.skip("GTK not available in test environment")
        from ui.views.event_cards import _build_task_segment
        seg = {"content": "[ ] plain task"}
        label = _build_task_segment(seg)
        markup = label.get_label()
        assert "☐" in markup
        assert "[ ]" not in markup

    def test_checked_box(self):
        if _gtk_skip():
            pytest.skip("GTK not available in test environment")
        from ui.views.event_cards import _build_task_segment
        seg = {"content": "[x] done"}
        label = _build_task_segment(seg)
        markup = label.get_label()
        assert "☑" in markup

    def test_javascript_link_blocked(self):
        """HIGH-6: javascript: links in tasks must be blocked."""
        if _gtk_skip():
            pytest.skip("GTK not available in test environment")
        from ui.views.event_cards import _build_task_segment
        seg = {"content": "[x] [click](javascript:alert(1))"}
        label = _build_task_segment(seg)
        retval = label.emit("activate-link", "javascript:alert(1)")
        assert retval is True

    def test_safe_link_allowed(self):
        """HIGH-6: https links in tasks must NOT be blocked."""
        if _gtk_skip():
            pytest.skip("GTK not available in test environment")
        from ui.views.event_cards import _build_task_segment
        seg = {"content": "[x] [safe](https://example.com)"}
        label = _build_task_segment(seg)
        retval = label.emit("activate-link", "https://example.com")
        assert retval is False

    def test_italic_and_code(self):
        if _gtk_skip():
            pytest.skip("GTK not available in test environment")
        from ui.views.event_cards import _build_task_segment
        seg = {"content": "[x] *italic* and `code`"}
        label = _build_task_segment(seg)
        markup = label.get_label()
        assert "<i>italic</i>" in markup
        assert "<tt>code</tt>" in markup

    def test_empty_content_returns_box(self):
        if _gtk_skip():
            pytest.skip("GTK not available in test environment")
        from ui.views.event_cards import _build_task_segment
        import gi
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk
        seg = {"content": ""}
        widget = _build_task_segment(seg)
        assert not isinstance(widget, Gtk.Label)

    def test_task_item_css_class(self):
        if _gtk_skip():
            pytest.skip("GTK not available in test environment")
        from ui.views.event_cards import _build_task_segment
        seg = {"content": "[x] task"}
        label = _build_task_segment(seg)
        classes = label.get_css_classes()
        assert "task-item" in classes
