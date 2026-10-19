# tests/test_feed_switch_regression.py — SPEC-41 Phase 1 (WU 10.1).
#
# The project-switch/close crash: feed_handler.clear_project() called
# `FeedTab.remove_card()`, deleted in the WU 10 GTK purge, and the
# AttributeError aborted the window's project-opened callback tuple.
#
# These tests drive the REAL FeedTab, never a mock. tests/test_feed_handler.py's
# MockFeedTab still defines remove_card/get_card_container — that mock mirroring
# a world that no longer exists is exactly why the suite stayed green against a
# crash. Nothing here subclasses or copies it.

import ast
import threading
from datetime import UTC, datetime
from pathlib import Path

import gi
import pytest

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk

from models.feed_card import AutoAcceptPrefs, FeedCardData

FEED_TAB_PATH = Path(__file__).resolve().parent.parent / "ui" / "views" / "feed_tab.py"


# ── Doubles ──────────────────────────────────────────────────────────────────


class _ImmediateGLib:
    """idle_add runs inline — the handler's UI hops are deterministic."""

    def idle_add(self, fn, *args, **kwargs):
        fn(*args, **kwargs)
        return 0

    def timeout_add(self, _ms, _cb):
        return 0

    def source_remove(self, _sid):
        return True


class _SyncThreading:
    """threading shim: Thread.start() runs the target on the calling thread."""

    Lock = threading.Lock
    Event = threading.Event

    class Thread:
        def __init__(self, target=None, name=None, daemon=None,
                     args=(), kwargs=None):
            self._target = target
            self._args = args or ()
            self._kwargs = kwargs or {}

        def start(self):
            if self._target is not None:
                self._target(*self._args, **self._kwargs)


class _StubSurface:
    """FeedSurface double: real FeedTab.attach_html_surface() drives this."""

    def __init__(self) -> None:
        self._doc_near_bottom = True
        self._order: list[str] = []
        self.removed: list[str] = []
        self.set_cards_calls: list[list] = []
        self.webview = Gtk.Label(label="webview")

    def card_ids(self) -> list[str]:
        return list(self._order)

    def set_cards(self, cards) -> None:
        self.set_cards_calls.append(list(cards))
        self._order = [c.card_id for c in cards if c.card_id]

    def upsert(self, card, *, prepend: bool = False) -> None:
        if card.card_id in self._order:
            return
        if prepend:
            self._order.insert(0, card.card_id)
        else:
            self._order.append(card.card_id)

    def remove(self, card_id: str) -> None:
        self._order = [cid for cid in self._order if cid != card_id]
        self.removed.append(card_id)

    def reveal(self, card_id: str) -> None:
        return None


def _card(card_id="card-1", project="proj-a") -> FeedCardData:
    return FeedCardData(
        card_type="diff",
        source="agent",
        title="Fix auth",
        body="+import x",
        author="Qat",
        timestamp=datetime(2026, 10, 19, tzinfo=UTC),
        project_name=project,
        card_id=card_id,
    )


@pytest.fixture
def surface(monkeypatch):
    """A real FeedTab whose document is a recording surface."""
    from ui.views import feed_surface

    stub = _StubSurface()
    monkeypatch.setattr(feed_surface, "create_feed_surface", lambda *a, **kw: stub)
    return stub


@pytest.fixture
def real_tab(surface):
    from ui.views.feed_tab import FeedTab

    tab = FeedTab()
    assert tab.attach_html_surface(lambda cid, action: None) is surface
    return tab


@pytest.fixture
def handler(real_tab, monkeypatch):
    """A real FeedHandler wired to the real FeedTab."""
    import ui.handlers.feed_handler as fh

    store = _MagicStore()
    monkeypatch.setattr(fh, "feed_store", store)
    monkeypatch.setattr(fh, "threading", _SyncThreading)

    h = fh.FeedHandler(GLib=_ImmediateGLib(), on_send_to_agent=lambda *a, **kw: None)
    h.set_feed_tab(real_tab)
    assert h._html_feed is True
    return h


class _MagicStore:
    """feed_store double: no disk I/O, no real feed.json."""

    FEED_WINDOW_DEFAULT = 300

    def load_feed(self, project_path):
        return []

    def load_feed_prefs(self, project_path):
        return AutoAcceptPrefs().to_dict()

    def append_feed_card(self, project_path, card):
        return None

    def save_feed_prefs(self, project_path, prefs):
        return None

    def get_live_window(self, project_path):
        return self.FEED_WINDOW_DEFAULT


# ── 1. Project switch / close raise nothing (REAL FeedTab) ───────────────────


class TestProjectSwitchAndClose:
    def test_clear_project_removes_rows_and_raises_nothing(self, handler, surface, real_tab):
        card_id = handler.add_card(_card(project="proj-a"))
        assert card_id in surface.card_ids()

        # The switch path (ui/feed/load.py:25) — this raised AttributeError.
        handler.clear_project("proj-a")

        assert surface.removed == [card_id]
        assert surface.card_ids() == []
        assert handler.get_card(card_id) is None

    def test_on_project_closed_raises_nothing(self, handler, surface):
        handler.add_card(_card(project="proj-b"))
        handler._active_project_name = "proj-b"

        handler.on_project_closed("proj-b")

        assert surface.card_ids() == []
        assert surface.set_cards_calls[-1] == []

    def test_real_on_project_opened_switch_completes(self, handler, surface):
        """The full path: on_project_opened(B) clears A through the real tab."""
        previous_id = handler.add_card(_card(project="proj-a"))
        handler._active_project_name = "proj-a"
        assert previous_id in surface.card_ids()

        handler.on_project_opened("proj-b", "/tmp/proj-b")

        assert handler._active_project_name == "proj-b"
        assert previous_id in surface.removed
        assert surface.card_ids() == []


# ── 2. remove_card forwards to remove_card_html ──────────────────────────────


class TestRemoveCardCompat:
    def test_remove_card_forwards_to_remove_card_html(self, real_tab, surface):
        real_tab.set_html_cards([_card("forward-me")])

        real_tab.remove_card("forward-me")

        assert surface.removed == ["forward-me"]
        assert surface.card_ids() == []

    def test_remove_card_calls_the_document_api(self, real_tab, monkeypatch):
        calls = []
        monkeypatch.setattr(real_tab, "remove_card_html", calls.append)

        real_tab.remove_card("routed-id")

        assert calls == ["routed-id"]

    def test_legacy_no_ops_keep_their_signatures(self, real_tab):
        # The pre-purge signatures: no caller may break on arity.
        assert real_tab.append_card(Gtk.Label(label="w"), "cid") is None
        assert real_tab.prepend_card(Gtk.Label(label="w"), "cid") is None
        assert real_tab.replace_card("cid", Gtk.Label(label="w")) is None
        assert real_tab.get_card_container() is None


# ── 3. Stale-state methods on a widget-less tab ──────────────────────────────


class TestStaleStateMethods:
    def test_is_above_viewport_is_false(self):
        from ui.views.feed_tab import FeedTab

        tab = FeedTab()  # no surface, no card container
        assert tab.is_above_viewport(Gtk.Label(label="card")) is False
        assert tab.is_above_viewport(object()) is False

    def test_hide_card_buttons_returns_without_raising(self):
        from ui.views.feed_tab import FeedTab

        tab = FeedTab()
        assert tab.hide_card_buttons("no-such-card", ["approve", "deny"]) is None
        assert tab.hide_card_buttons("anything", []) is None


# ── 4. No production reference to the deleted GTK state ──────────────────────


class TestPurgedStateIsGone:
    def test_no_attribute_access_to_deleted_maps(self):
        """AST-level: prose/comments may name them, code may not read them."""
        tree = ast.parse(FEED_TAB_PATH.read_text(encoding="utf-8"))
        attrs = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}

        assert "_cards_by_id" not in attrs | names
        assert "_card_container" not in attrs | names
