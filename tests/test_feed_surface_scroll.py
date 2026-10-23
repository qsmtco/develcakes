# tests/test_feed_surface_scroll.py — SPEC-FEED-HTML-SURFACE, chunk 1.
# The document scrolls itself; Python only remembers and restores.
# The surface is built with __new__ (existing pattern in tests/test_feed_html.py),
# so no real WebKit widget is instantiated — the web view is a recording double.

import re
from datetime import UTC, datetime

import gi
import pytest

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk

from models.feed_card import FeedCardData
from render.feed_html import FEED_SCRIPT
from ui.views.feed_surface import _BOTTOM_SCRIPT, FeedSurface, _restore_script

try:
    from ui.views.feed_surface import WebKit
except ImportError:  # pragma: no cover — module always exports the name
    WebKit = None


class _FakeView:
    """Records the two calls the surface makes on a web view."""

    def __init__(self) -> None:
        self.evals: list[str] = []
        self.loads: list[str] = []

    def evaluate_javascript(self, script, *args):
        self.evals.append(script)

    def load_html(self, document, base_uri):
        self.loads.append(document)


class _FakeValue:
    """Stands in for a JSC Value (the message handler's first argument)."""

    def __init__(self, payload: str) -> None:
        self._payload = payload

    def to_json(self, _indent) -> str:
        return self._payload


def _card(card_id="card-1", body="hello") -> FeedCardData:
    return FeedCardData(
        card_type="system",
        source="system",
        title="Title",
        body=body,
        author="Ada",
        timestamp=datetime(2026, 10, 17, tzinfo=UTC),
        project_name="proj",
        card_id=card_id,
    )


def _surface(near_bottom=True, y=None) -> FeedSurface:
    surface = FeedSurface.__new__(FeedSurface)
    surface._on_action = lambda cid, action: None
    surface._order = []
    surface._cards = {}
    surface._html = {}
    surface._body = {}
    surface._revealed = set()
    surface._context_open = set()
    surface._dirty = False
    surface._render_source = None
    surface._load_in_flight = False
    surface._doc_near_bottom = near_bottom
    surface._doc_y = y
    surface._scroll_after = False
    surface._webview = _FakeView()
    return surface


def _finish(surface: FeedSurface) -> None:
    surface._on_load_changed(surface._webview, WebKit.LoadEvent.FINISHED)


class TestScrollStateMessages:
    def test_scroll_state_updates_follow_intent_and_offset(self):
        surface = _surface()
        surface._on_scroll_state({"action": "scroll_state", "near_bottom": False, "y": 320})
        assert surface._doc_near_bottom is False
        assert surface._doc_y == 320.0

        surface._on_scroll_state({"near_bottom": True, "y": 0})
        assert surface._doc_near_bottom is True
        assert surface._doc_y == 0.0

    def test_message_handler_routes_scroll_state_without_a_card(self):
        surface = _surface()
        payload = '{"action": "scroll_state", "near_bottom": false, "y": 41.5, "h": 900}'
        surface._on_script_message(None, _FakeValue(payload))
        assert surface._doc_near_bottom is False
        assert surface._doc_y == 41.5

    def test_bad_scroll_state_values_are_ignored(self):
        surface = _surface()
        surface._on_scroll_state({"near_bottom": "no", "y": "far"})
        assert surface._doc_near_bottom is True
        assert surface._doc_y is None
        # bool is an int subclass — a boolean y must not become an offset.
        surface._on_scroll_state({"y": True})
        assert surface._doc_y is None

    def test_flush_takes_follow_intent_from_the_document(self):
        surface = _surface(near_bottom=False, y=200.0)
        surface._cards = {"card-1": _card()}
        surface._order = ["card-1"]
        surface._flush()
        assert surface._scroll_after is False
        assert surface._webview.loads and len(surface._webview.loads) == 1

        surface = _surface(near_bottom=True)
        surface._cards = {"card-1": _card()}
        surface._order = ["card-1"]
        surface._flush()
        assert surface._scroll_after is True


@pytest.mark.skipif(WebKit is None, reason="WebKit introspection unavailable")
class TestScrollAfterLoad:
    def test_flush_near_bottom_scrolls_to_bottom_after_load(self):
        surface = _surface(near_bottom=True)
        surface._cards = {"card-1": _card()}
        surface._order = ["card-1"]
        surface._flush()
        assert surface._load_in_flight is True
        assert surface._webview.evals == []  # nothing before the load finishes

        _finish(surface)
        assert surface._load_in_flight is False
        assert surface._webview.evals == [_BOTTOM_SCRIPT]
        assert "document.documentElement.scrollHeight" in surface._webview.evals[0]

    def test_flush_scrolled_up_restores_offset_and_does_not_jump(self):
        surface = _surface(near_bottom=False, y=340.0)
        surface._cards = {"card-1": _card()}
        surface._order = ["card-1"]
        surface._flush()

        _finish(surface)
        assert surface._webview.evals == [_restore_script(340.0)]
        assert "scrollHeight" not in surface._webview.evals[0]
        assert "340.0" in surface._webview.evals[0]

    def test_no_remembered_offset_and_not_following_scrolls_nowhere(self):
        surface = _surface(near_bottom=False, y=None)
        surface._cards = {"card-1": _card()}
        surface._order = ["card-1"]
        surface._flush()
        _finish(surface)
        assert surface._webview.evals == []

    def test_follow_intent_is_consumed_by_one_load(self):
        surface = _surface(near_bottom=True)
        surface._cards = {"card-1": _card()}
        surface._order = ["card-1"]
        surface._flush()
        _finish(surface)
        assert surface._scroll_after is False
        assert len(surface._webview.evals) == 1


class TestFeedScriptIsStatic:
    def test_no_format_placeholders(self):
        assert not re.search(r"\{[A-Za-z_][A-Za-z0-9_.]*\}", FEED_SCRIPT)
        assert "%s" not in FEED_SCRIPT
        assert "%(" not in FEED_SCRIPT

    def test_script_posts_scroll_state_but_carries_no_card_data(self):
        assert "scroll_state" in FEED_SCRIPT
        assert "near_bottom" in FEED_SCRIPT
        assert "card_id" in FEED_SCRIPT  # click channel only
        assert "card-1" not in FEED_SCRIPT
        assert "document.body.innerHTML" not in FEED_SCRIPT

    def test_bottom_and_restore_scripts_are_document_scrolls(self):
        assert "window.scrollTo(0, document.documentElement.scrollHeight)" in _BOTTOM_SCRIPT
        assert _restore_script(12) == "window.scrollTo(0, 12.0);"


class _StubSurface:
    """Stands in for FeedSurface in FeedTab.attach_html_surface."""

    def __init__(self) -> None:
        self._doc_near_bottom = True
        self.webview = Gtk.Label(label="webview")
        self.set_cards_calls: list[list] = []

    def set_cards(self, cards) -> None:
        self.set_cards_calls.append(list(cards))


def _attached_tab(monkeypatch, stub: _StubSurface):
    """A real FeedTab whose attach_html_surface returns `stub`."""
    from ui.views import feed_surface

    monkeypatch.setattr(feed_surface, "create_feed_surface", lambda *a, **kw: stub)
    from ui.views.feed_tab import FeedTab

    tab = FeedTab()
    assert tab.attach_html_surface(lambda cid, action: None) is stub
    return tab


class TestAttachHtmlSurfaceIntegration:
    def test_attach_flips_vertical_policy_to_never(self, monkeypatch):
        """The document owns vertical scrolling; no second GTK scrollbar."""
        stub = _StubSurface()
        tab = _attached_tab(monkeypatch, stub)
        assert isinstance(tab._feed_scroll, Gtk.ScrolledWindow)
        assert tab._feed_scroll.get_policy() == (
            Gtk.PolicyType.EXTERNAL,
            Gtk.PolicyType.NEVER,
        )
        # A non-scrollable stub is wrapped in a Gtk.Viewport, so assert the
        # stub lives in the scroll's child tree rather than as its direct child.
        child = tab._feed_scroll.get_child()
        assert child is stub.webview or child.get_child() is stub.webview

    def test_attach_drops_the_legacy_gtk_hooks(self, monkeypatch):
        """is_near_bottom / on_load_finished are no longer wired."""
        from ui.views import feed_surface

        seen = {}

        def _factory(on_action, **kwargs):
            seen.update(kwargs)
            return _StubSurface()

        monkeypatch.setattr(feed_surface, "create_feed_surface", _factory)
        from ui.views.feed_tab import FeedTab

        FeedTab().attach_html_surface(lambda cid, action: None)
        assert seen["is_near_bottom"] is None
        assert seen["on_load_finished"] is None

    def test_is_near_bottom_delegates_to_the_document(self, monkeypatch):
        """Under PolicyType.NEVER the vadjustment cannot answer this."""
        stub = _StubSurface()
        tab = _attached_tab(monkeypatch, stub)
        stub._doc_near_bottom = False
        assert tab.is_near_bottom() is False
        stub._doc_near_bottom = True
        assert tab.is_near_bottom() is True

    def test_schedule_smart_scroll_is_inert_with_a_surface(self, monkeypatch):
        """The follow decision lives in FeedSurface._flush, not the GTK path."""
        stub = _StubSurface()
        tab = _attached_tab(monkeypatch, stub)
        tab.schedule_smart_scroll_to_bottom()
        assert tab._scroll_handler_id is None
        assert tab._scroll_timeout_id is None
        assert tab._settle_source is None

    def test_map_handler_does_not_arm_settle_with_a_surface(self, monkeypatch):
        """SPEC-57 §2g: with an HTML surface attached the map handler no-ops
        instead of arming the GTK scroll settle."""
        stub = _StubSurface()
        tab = _attached_tab(monkeypatch, stub)
        tab._was_near_bottom = True
        tab._on_scroll_mapped(tab._feed_scroll)
        assert tab._scroll_handler_id is None


class TestSetCardsResetsScrollState:
    """SPEC-41 P3 (a): a new document is a new reading context.

    `_doc_near_bottom` / `_doc_y` are reported by the PREVIOUS document's
    FEED_SCRIPT. Without a reset, a project switch inherits the old
    project's offset AND its follow-off state: the new project opens
    mid-page and its new cards land below the fold forever.
    """

    def _frozen(self, near_bottom, y) -> FeedSurface:
        """A surface whose _schedule() will not queue a real GLib idle.

        `_load_in_flight = True` is the production "a document load is in
        flight" state — _schedule() sets _dirty and returns. The fields under
        test are written by set_cards itself, before any scheduling.
        """
        surface = _surface(near_bottom=near_bottom, y=y)
        surface._load_in_flight = True
        return surface

    def test_set_cards_resets_scrolled_up_state(self):
        surface = self._frozen(near_bottom=False, y=512.5)
        surface.set_cards([_card()])
        assert surface._doc_near_bottom is True
        assert surface._doc_y is None
        assert surface.card_ids() == ["card-1"]

    def test_set_cards_resets_even_with_no_remembered_offset(self):
        surface = self._frozen(near_bottom=False, y=None)
        surface.set_cards([_card("a"), _card("b")])
        assert surface._doc_near_bottom is True
        assert surface._doc_y is None
        assert surface.card_ids() == ["a", "b"]  # oldest-first preserved

    @pytest.mark.skipif(WebKit is None, reason="WebKit introspection unavailable")
    def test_new_document_follows_the_bottom(self):
        """User-visible consequence: after a switch the next load lands on
        the newest card instead of restoring the previous project's offset."""
        surface = self._frozen(near_bottom=False, y=900.0)
        surface.set_cards([_card()])
        surface._load_in_flight = False
        surface._flush()
        assert surface._scroll_after is True
        _finish(surface)
        assert surface._webview.evals == [_BOTTOM_SCRIPT]
