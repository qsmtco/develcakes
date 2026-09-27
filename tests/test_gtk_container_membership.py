"""
Tests for utils.gtk_containers.is_in_container().

Documents the PyGObject container membership bug (``widget in gtk_box`` raises
``TypeError``) and verifies the ``is_in_container()`` helper correctly walks
the sibling chain via ``get_first_child()`` / ``get_next_sibling()``.

These tests use only pure-Python fakes — no GTK widget construction.
"""

import pytest
from utils.gtk_containers import is_in_container


# ── Fakes (revised per spec §3.5.1, 2026-07-28) ───────────────────────


class FakeChildWidget:
    """Minimal widget stand-in with identity-based comparison.

    Models GTK4's sibling chain: each widget holds a back-reference to its
    parent container and implements the no-arg get_next_sibling() by looking
    up its position in the container's child list.
    """
    def __init__(self):
        self._parent_container = None

    def get_next_sibling(self):
        """Return the next sibling after self, or None (mirrors Gtk.Widget)."""
        if self._parent_container is None:
            return None
        children = self._parent_container._children
        try:
            idx = children.index(self)
        except ValueError:
            return None
        return children[idx + 1] if idx + 1 < len(children) else None


class FakeGtkBoxNoContains:
    """
    Reproduces the real GTK bug: no ``__contains__``, no ``__iter__``.

    ``widget in fake_box`` raises ``TypeError``, exactly like ``widget in Gtk.Box()``
    does in PyGObject. This proves the bug class without mocking the symptom.
    """
    def __init__(self):
        self._children = []

    def append(self, widget):
        widget._parent_container = self
        self._children.append(widget)

    def remove(self, widget):
        widget._parent_container = None
        self._children.remove(widget)

    def get_first_child(self):
        return self._children[0] if self._children else None


# ── Group A: Document the bug class (1 test) ─────────────────────────


def test_widget_in_gtk_box_raises_type_error():
    """
    ``widget in gtk_box`` raises TypeError because PyGObject does not
    wire ``__contains__`` onto GTK containers. Use ``is_in_container()``
    instead.
    """
    box = FakeGtkBoxNoContains()
    widget = FakeChildWidget()
    box.append(widget)
    with pytest.raises(TypeError):
        _ = widget in box


# ── Group B: Unit tests for is_in_container (9 tests) ────────────────


class TestIsInContainer:
    """Tests for utils.gtk_containers.is_in_container()."""

    def test_widget_present_first_child(self):
        """Widget is the first (and only) child → True."""
        box = FakeGtkBoxNoContains()
        w = FakeChildWidget()
        box.append(w)
        assert is_in_container(w, box) is True

    def test_widget_present_middle_child(self):
        """Widget is the 3rd of 5 children → True."""
        box = FakeGtkBoxNoContains()
        widgets = [FakeChildWidget() for _ in range(5)]
        for w in widgets:
            box.append(w)
        assert is_in_container(widgets[2], box) is True

    def test_widget_present_last_child(self):
        """Widget is the last child → True (walk reaches the end)."""
        box = FakeGtkBoxNoContains()
        w1, w2 = FakeChildWidget(), FakeChildWidget()
        box.append(w1)
        box.append(w2)
        assert is_in_container(w2, box) is True

    def test_widget_absent(self):
        """Container has children, widget not among them → False."""
        box = FakeGtkBoxNoContains()
        box.append(FakeChildWidget())
        box.append(FakeChildWidget())
        other = FakeChildWidget()
        assert is_in_container(other, box) is False

    def test_widget_after_remove(self):
        """Widget was once a child but was removed → False."""
        box = FakeGtkBoxNoContains()
        w = FakeChildWidget()
        box.append(w)
        box.remove(w)
        assert is_in_container(w, box) is False

    def test_none_widget(self):
        """widget=None → False."""
        box = FakeGtkBoxNoContains()
        assert is_in_container(None, box) is False

    def test_none_container(self):
        """container=None → False."""
        w = FakeChildWidget()
        assert is_in_container(w, None) is False

    def test_both_none(self):
        """Both None → False."""
        assert is_in_container(None, None) is False

    def test_empty_container(self):
        """Container has no children → False."""
        box = FakeGtkBoxNoContains()
        w = FakeChildWidget()
        assert is_in_container(w, box) is False


# ── Group C: Static regression checks (4 tests) ──────────────────────


class TestStaticRegression:
    """Source-level checks that the old broken patterns are gone."""

    CHAT_RENDER_PATH = "ui/handlers/chat_render_handler.py"
    FEED_TAB_PATH = "ui/views/feed_tab.py"

    def test_chat_render_handler_no_old_pattern(self):
        """The 'if sb.bubble in sb.container' pattern is gone."""
        src = self._read(self.CHAT_RENDER_PATH)
        assert "sb.bubble in sb.container" not in src, \
            f"Old pattern 'sb.bubble in sb.container' still present in {self.CHAT_RENDER_PATH}"

    def test_feed_tab_no_old_patterns(self):
        """All 5 'in self._card_container' patterns are gone."""
        src = self._read(self.FEED_TAB_PATH)
        for pattern in ["widget in self._card_container",
                        "self._empty_widget in self._card_container",
                        "old_widget not in self._card_container"]:
            assert pattern not in src, \
                f"Old pattern '{pattern}' still present in {self.FEED_TAB_PATH}"

    # Lineage: test_is_in_container_imported_in_chat_render RETIRED — its
    # subject DIED (not moved): chat_render_handler.py lost the
    # is_in_container import with the Pango bubble path in SP4 (2a057adc,
    # render-handler repoint). The feed_tab twin below stays live.

    def test_is_in_container_imported_in_feed_tab(self):
        """is_in_container is imported in feed_tab.py."""
        src = self._read(self.FEED_TAB_PATH)
        assert "from utils.gtk_containers import is_in_container" in src

    def test_dispatch_has_exception_logging(self):
        """_dispatch's _wrap has try/except with _logger.exception.

        Scoped to the _wrap function body specifically — a coarse 'try:' check
        would be too weak (chat_render_handler.py has 5 unrelated try: blocks).
        This extracts the _wrap body and verifies the exception-logging pattern
        co-occurs there.
        """
        src = self._read(self.CHAT_RENDER_PATH)
        # Extract the _wrap function body specifically
        assert "def _wrap():" in src, "_wrap function not found in _dispatch"
        wrap_start = src.find("def _wrap():")
        wrap_end = src.find("self._GLib.idle_add(_wrap)", wrap_start)
        assert wrap_end != -1, "_wrap function body end marker not found"
        wrap_body = src[wrap_start:wrap_end]
        assert "try:" in wrap_body, "try: block missing from _wrap"
        assert "except (KeyboardInterrupt, SystemExit):" in wrap_body, \
            "KeyboardInterrupt/SystemExit re-raise missing from _wrap"
        assert "except Exception:" in wrap_body, \
            "except Exception clause missing from _wrap"
        assert "_logger.exception" in wrap_body, \
            "_logger.exception call missing from _wrap"

    @staticmethod
    def _read(path):
        with open(path) as f:
            return f.read()