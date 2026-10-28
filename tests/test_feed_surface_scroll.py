# tests/test_feed_surface_scroll.py — SPEC-FEED-HTML-SURFACE, chunk 1.
# The document scrolls itself; Python only remembers and restores.
# The surface is built with __new__ (existing pattern in tests/test_feed_html.py),
# so no real WebKit widget is instantiated — the web view is a recording double.

import re
from datetime import UTC, datetime

import gi
import pytest

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk

from models.feed_card import FeedCardData
from render.feed_html import FEED_SCRIPT, render_feed_card
from ui.views.feed_surface import (
    _BOTTOM_SCRIPT,
    FeedSurface,
    _ops_script,
    _restore_script,
)

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

    def evaluate_javascript_finish(self, result):
        """The async result of evaluate_javascript resolves to the JSC value.

        `_FakeValue` stands in for that value, so the finished value is the
        result object itself (a real WebKit view would unwrap the task).
        """
        return result

    def load_html(self, document, base_uri):
        self.loads.append(document)


class _FakeValue:
    """Stands in for a JSC Value (script-message payload and eval result)."""

    def __init__(self, payload: str) -> None:
        self._payload = payload

    def to_json(self, _indent) -> str:
        return self._payload

    def to_string(self) -> str:
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
    # SPEC-57 §2f patch state. `_load_epoch` starts equal to `_epoch` so a
    # `_finish()` in the legacy scroll tests is not mistaken for a stale load.
    surface._dom_ready = False
    surface._dom_ids = []
    surface._dom_html = {}
    surface._pending_dom_ids = []
    surface._pending_dom_html = {}
    surface._patch_in_flight = False
    surface._epoch = 0
    surface._load_epoch = 0
    surface._queued_reload = False
    surface._last_ops = None
    surface._patch_count = 0
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
        # The GTK scroll settle is GONE (SPEC-58 §2f): the tab must not
        # carry the machinery that used to arm it.
        assert not hasattr(tab, "_scroll_handler_id")
        assert not hasattr(tab, "_scroll_timeout_id")
        assert not hasattr(tab, "_settle_source")


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


class TestFeedPatchFlush:
    """SPEC-57 §2h items 2–6 — the DOM patch path."""

    def _ready(self, dom, order):
        """A surface whose DOM is loaded and whose `_dom_html` beliefs match
        the fragments `_flush` will render (so the plan is additive)."""
        surface = _surface()
        surface._dom_ready = True
        surface._dom_ids = list(dom)
        cards = {cid: _card(cid) for cid in order}
        surface._cards = cards
        surface._order = list(order)
        surface._html = {}
        surface._dom_html = {
            cid: render_feed_card(cards[cid]) for cid in dom
        }
        return surface

    def test_flush_patches_when_dom_ready_emits_one_eval_not_load(self):
        surface = self._ready(["card-1"], ["card-1", "card-2"])
        surface._flush()
        assert surface._webview.loads == []
        assert len(surface._webview.evals) == 1
        assert surface._patch_in_flight is True
        # The reader was following; the patch script handles the scroll inside
        # the same single eval. Commit the reported DOM ids now.
        surface._on_patch_finished(
            surface._webview,
            _FakeValue('[["card-1","card-2"],"followed"]'),
            surface._epoch,
        )
        assert surface._dom_ids == ["card-1", "card-2"]
        assert surface._patch_in_flight is False

    def test_flush_noop_when_dom_already_matches(self):
        surface = self._ready(["card-1"], ["card-1"])
        surface._flush()
        assert surface._webview.evals == []
        assert surface._webview.loads == []
        assert surface._dirty is False

    def test_flush_falls_back_to_load_when_plan_returns_none(self):
        # prepend+append mix -> plan returns None -> full document load
        surface = self._ready(
            ["card-1"], ["card-0", "card-1", "card-2"]
        )
        surface._flush()
        assert surface._webview.evals == []
        assert len(surface._webview.loads) == 1
        assert surface._load_in_flight is True

    def test_patch_callback_missing_recovers_with_full_reload(self):
        surface = self._ready(["card-1"], ["card-1", "card-2"])
        surface._flush()
        assert surface._patch_in_flight is True
        surface._on_patch_finished(
            surface._webview, _FakeValue("null"), surface._epoch
        )
        assert surface._dom_ready is False
        assert surface._dirty is True
        assert surface._patch_in_flight is False
        assert surface._queued_reload is True

    def test_stale_epoch_patch_callback_is_ignored(self):
        surface = self._ready(["card-1"], ["card-1", "card-2"])
        surface._flush()
        before = list(surface._dom_ids)
        surface._on_patch_finished(
            surface._webview,
            _FakeValue('[["card-1","card-2"],"followed"]'),
            surface._epoch + 1,
        )
        assert surface._dom_ids == before
        assert surface._patch_in_flight is True

    def test_set_cards_bumps_epoch_and_clears_in_flight_patch(self):
        surface = self._ready(["card-1"], ["card-1", "card-2"])
        surface._flush()
        assert surface._patch_in_flight is True
        surface.set_cards([_card("card-3")])
        assert surface._epoch == 1
        assert surface._patch_in_flight is False
        assert surface._dom_ready is False
        assert surface._dom_ids == []
        assert surface.card_ids() == ["card-3"]

    def test_set_cards_bumps_epoch_and_clears_in_flight_load(self):
        surface = _surface()
        surface._load_in_flight = True
        surface._load_epoch = 0
        surface.set_cards([_card("card-3")])
        assert surface._epoch == 1
        assert surface._load_in_flight is False
        assert surface._load_epoch is None
        assert surface._dom_ready is False

    def test_stale_patch_callback_after_set_cards_leaves_new_state(self):
        surface = self._ready(["card-1"], ["card-1", "card-2"])
        surface._flush()
        surface.set_cards([_card("card-3")])
        surface._on_patch_finished(
            surface._webview,
            _FakeValue('[["card-1","card-2"],"held"]'),
            0,
        )
        assert surface._dom_ids == []
        assert surface.card_ids() == ["card-3"]


class TestPlanFeedOps:
    """SPEC-57 §2f `_plan_feed_ops` traced cases."""

    def _surface_with(self, dom, order, html_ids=None):
        surface = _surface()
        surface._dom_ready = True
        surface._dom_ids = list(dom)
        surface._order = list(order)
        surface._html = {
            cid: f"<article data-card-id={cid}></article>"
            for cid in (html_ids if html_ids is not None else order)
        }
        surface._dom_html = {
            cid: surface._html[cid]
            for cid in dom
            if cid in surface._html
        }
        return surface

    def test_identical_returns_empty_list(self):
        assert self._surface_with(["a", "b"], ["a", "b"])._plan_feed_ops() == []

    def test_prepend_only(self):
        ops = self._surface_with(["b", "c"], ["a", "b", "c"])._plan_feed_ops()
        assert ops is not None and len(ops) == 1
        assert ops[0]["op"] == "prepend"
        assert ops[0]["id"] == "a"

    def test_append_only(self):
        ops = self._surface_with(["a", "b"], ["a", "b", "c"])._plan_feed_ops()
        assert ops is not None and len(ops) == 1
        assert ops[0]["op"] == "append"
        assert ops[0]["id"] == "c"

    def test_mixed_prepend_and_append_returns_none(self):
        assert self._surface_with(["b"], ["a", "b", "c"])._plan_feed_ops() is None

    def test_reorder_returns_none(self):
        assert self._surface_with(["a", "b"], ["b", "a"])._plan_feed_ops() is None

    def test_duplicate_ids_return_none(self):
        assert self._surface_with(["a"], ["a", "a"])._plan_feed_ops() is None

    def test_all_gone_plus_new_returns_none(self):
        assert self._surface_with(["a"], ["b"])._plan_feed_ops() is None

    def test_missing_html_returns_none(self):
        surface = self._surface_with(["a"], ["a", "b"], html_ids=["a"])
        assert surface._plan_feed_ops() is None

    def test_same_order_changed_html_emits_replace_op(self):
        """FIX 2 (SPEC-57): `order == dom` with changed content must return a
        replace DICT (not a bare card-id string, which would crash
        `_ops_script`'s `op.get("op")`)."""
        surface = self._surface_with(["a", "b"], ["a", "b"])
        surface._dom_html = {
            "a": "<article data-card-id=a>old</article>",
            "b": surface._html["b"],
        }
        ops = surface._plan_feed_ops()
        assert ops == [{"op": "replace", "id": "a", "html": surface._html["a"]}]


class TestOpsScript:
    """SPEC-57 §2f `_ops_script` constraints."""

    def test_uses_removechild_replacechild_not_dot_methods(self):
        script = _ops_script([
            {"op": "remove", "id": "a"},
            {
                "op": "replace",
                "id": "b",
                "html": "<article data-card-id=b></article>",
            },
        ])
        assert "removeChild" in script
        assert ".remove(" not in script
        assert "replaceChild" in script
        assert ".replaceWith(" not in script

    def test_inserts_get_feed_card_enter_replacements_do_not(self):
        script = _ops_script([
            {
                "op": "prepend",
                "id": "a",
                "html": "<article data-card-id=a></article>",
            },
            {
                "op": "replace",
                "id": "b",
                "html": "<article data-card-id=b></article>",
            },
        ])
        assert script.count("feed-card-enter") == 1

    def test_near_bottom_slack_matches_feed_script(self):
        assert "<= 80" in _ops_script([])

    def test_id_with_quote_never_builds_a_css_selector(self):
        # SPEC: never build a CSS selector from a card id — a quote or `]`
        # would make querySelector throw (SyntaxError), not return null.
        script = _ops_script([
            {"op": "remove", "id": 'a"b'},
            {
                "op": "replace",
                "id": 'c]d',
                "html": "<article data-card-id=x></article>",
            },
        ])
        assert "querySelectorAll('article[data-card-id]')" in script
        assert "querySelector(" not in script
        assert '[data-card-id="a\"b"]' not in script
        assert '[data-card-id="c]d"]' not in script

    def test_prepend_generates_scroll_height_delta_correction_and_held_outcome(self):
        script = _ops_script([
            {
                "op": "prepend",
                "id": "a",
                "html": "<article data-card-id=a></article>",
            },
        ])
        assert "y + grew" in script
        assert "'held'" in script
        assert "'followed'" in script

    def test_replace_before_prepend_in_planner_order_cannot_detach_anchor(self):
        """FIX 3 (SPEC-57 §2f): `_ops_script` is four ordered phases —
        removals, anchor capture, inserts (prepends before appends), replaces
        LAST. The planner may emit `[replace c1, prepend c3]` (replace first)
        where `c1` could be the anchor `feed.firstChild`; the program must
        still run the prepend's `insertBefore` before the replace's
        `replaceChild`. If the replace fired first it would detach the anchor
        and `insertBefore(node, anchor)` would throw NotFoundError."""
        script = _ops_script([
            {
                "op": "replace",
                "id": "c1",
                "html": "<article data-card-id=c1></article>",
            },
            {
                "op": "prepend",
                "id": "c3",
                "html": "<article data-card-id=c3></article>",
            },
        ])
        anchor = script.index("var anchor = feed.firstChild;")
        insert_before = script.index("feed.insertBefore(nodeP, anchor);")
        replace_child = script.index("feed.replaceChild(nodeR, old);")
        assert anchor < insert_before < replace_child


@pytest.mark.skipif(WebKit is None, reason="WebKit introspection unavailable")
class TestRealWebViewPatchEndToEnd:
    """FIX 1 (SPEC-57): the production patch callback shape on a REAL WebKit
    view (not the `__new__` fakes elsewhere in this module).

    The e2e proof: seed one card via `load_html`, then upsert a second card.
    The second card must be applied by exactly one `evaluate_javascript` (the
    patch) with NO second `load_html` — proven by a page-global marker that a
    full-document reload would destroy. The DOM must match `_dom_ids`.

    Needs a display (run under `xvfb-run -a`); conftest skips the module
    headless because this class constructs a `Gtk.Window` / web view.
    """

    @staticmethod
    def _present(surface):
        win = Gtk.Window()
        # D1 (round-3 audit): FeedSurface is a controller, not a Gtk.Widget —
        # mount the webview it owns (FeedTab does the same via attach).
        win.set_child(surface.webview)
        win.set_default_size(640, 480)
        win.present()
        return win

    @staticmethod
    def _pump(sec):
        ctx = GLib.MainContext.default()
        import time

        end = time.time() + sec
        while time.time() < end:
            while ctx.pending():
                ctx.iteration(False)
            time.sleep(0.02)

    def _await_ready(self, surface, timeout=10):
        import time

        end = time.time() + timeout
        while time.time() < end and not surface._dom_ready:
            self._pump(0.05)
        assert surface._dom_ready, "feed document never reached FINISHED commit"

    def _eval_main(self, surface, code, timeout=6):
        # D2: the WebKitGTK eval callback is (view, result, user_data).
        # D3: the timeout guard is REMOVED after the loop so a later eval's
        # MainLoop cannot be quit early by an earlier eval's stale timeout.
        box = {}
        loop = GLib.MainLoop()

        def cb(v, r, _data):
            try:
                box["v"] = v.evaluate_javascript_finish(r).to_string()
            except Exception:  # noqa: BLE001
                box["v"] = None
            loop.quit()

        tid = GLib.timeout_add_seconds(timeout, lambda: (loop.quit(), False)[1])
        try:
            surface._webview.evaluate_javascript(code, -1, None, None, None, cb, None)
            loop.run()
        finally:
            GLib.source_remove(tid)
        return box.get("v")

    def test_new_card_patches_in_place_no_reload(self):
        actions = []
        s = FeedSurface(
            lambda cid, action: actions.append((cid, action)),
            is_near_bottom=None,
            on_load_finished=None,
        )
        win = self._present(s)
        try:
            # Initial document: a real load_html -> FINISHED commit.
            s.set_cards([_card("card-1", "one")])
            self._await_ready(s)
            assert s._dom_ids == ["card-1"]
            assert s._dom_ready is True

            # Plant a page-global marker. A second `load_html` (a reload)
            # would replace the document and lose it — this is the proof that
            # the upcoming card change patches the SAME page.
            assert self._eval_main(s, "window.__alive = 1; 'ok'") == "ok"

            evals_before = s._patch_count
            s.upsert(_card("card-2", "two"))

            import time

            end = time.time() + 8
            while time.time() < end and len(s._dom_ids) < 2:
                self._pump(0.05)

            # Exactly one patch eval, no recovery, no reload.
            assert s._patch_count == evals_before + 1, "expected exactly one patch eval"
            assert s._dom_ids == ["card-1", "card-2"]
            assert s._dom_ready is True
            assert s._load_in_flight is False
            assert s._queued_reload is False

            # Live document agrees: the marker survived (no reload) and the
            # DOM now holds both cards in order.
            got = self._eval_main(
                s,
                "JSON.stringify({alive: window.__alive || 0, "
                "ids: Array.prototype.map.call("
                "document.querySelectorAll('article[data-card-id]'), "
                "function (n) { return n.getAttribute('data-card-id'); })})",
            )
            assert got == '{"alive":1,"ids":["card-1","card-2"]}'
        finally:
            win.close()
            win.destroy()


class TestCanvasColor:
    """FIX 5a (SPEC-57 §2h item 1): `_apply_canvas_color` applies a parsed,
    opaque background to a web view — it must not be a no-op."""

    def test_apply_canvas_color_sets_opaque_background(self):
        from ui.views.feed_surface import _apply_canvas_color

        seen = {}

        class _FakeView:
            def set_background_color(self, rgba):
                seen["rgba"] = rgba

        _apply_canvas_color(_FakeView())
        assert seen["rgba"].alpha == 1.0
        assert seen["rgba"].red > 0
