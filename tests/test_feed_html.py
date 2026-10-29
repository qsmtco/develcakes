# tests/test_feed_html.py — SPEC-FEED-HTML-SURFACE.
# The renderer is pure. Handler tests use a fake document, not WebKit.
#
# Headless-safe: this suite never constructs a GTK widget — it reads
# RENDERED_BODY_LIMIT from the view module and drives FeedHandler with stub
# GLib/surfaces. Opt out of the conftest display guard (same marker as
# tests/test_perf_feed.py) so these pins run in the headless battery.
GTK_HEADLESS_SAFE = True

from datetime import UTC, datetime

from models.conversation_snapshot import ConversationSnapshot, SnapshotMessage
from models.feed_card import FeedCardData
from render.feed_html import (
    FEED_CSS,
    FEED_RENDERED_BODY_LIMIT,
    FEED_SCRIPT,
    render_feed_card,
    render_feed_document,
)


def _card(**kwargs) -> FeedCardData:
    base = {
        "card_type": "system",
        "source": "system",
        "title": "Title",
        "body": "hello",
        "author": "Ada",
        "timestamp": datetime(2026, 10, 17, tzinfo=UTC),
        "project_name": "proj",
        "card_id": "card-1",
    }
    base.update(kwargs)
    return FeedCardData(**base)


class TestRenderFeedCard:
    def test_body_html_is_escaped_and_script_cannot_break_out(self):
        body = '<div>x</div></script><script>alert(1)</script>'
        doc = render_feed_document([render_feed_card(_card(body=body))])
        assert "&lt;div&gt;x&lt;/div&gt;" in doc
        assert "<div>x</div>" not in doc
        assert doc.count("<script>") == 1
        assert "&gt;alert(1)&lt;" in doc
        assert "<script>alert(1)</script>" not in doc
        assert FEED_SCRIPT.strip()[:12] in doc
        assert "card.body" not in FEED_SCRIPT

    def test_limit_matches_the_gtk_constant(self):
        """The render-side cap is pinned at 2000 (SPEC-58 §2c: the old
        ui.views.feed_card duplicate and its equality pin are gone)."""
        assert FEED_RENDERED_BODY_LIMIT == 2000

    def test_long_mono_body_shows_exact_hidden_count(self):
        text = "z" * (FEED_RENDERED_BODY_LIMIT + 7)
        out = render_feed_card(_card(card_type="git_commit", body=text))
        assert "feed-body-mono" in out
        assert "… 7 more characters" in out
        assert "z" * FEED_RENDERED_BODY_LIMIT in out
        assert "z" * (FEED_RENDERED_BODY_LIMIT + 1) not in out

    def test_reveal_shows_full_body_still_escaped(self):
        text = "<tag>" + ("z" * FEED_RENDERED_BODY_LIMIT)
        out = render_feed_card(_card(body=text), revealed=True)
        assert "&lt;tag&gt;" in out
        assert "more characters" not in out
        assert "<tag>" not in out

    def test_approval_reveal_title_and_button_words(self):
        out = render_feed_card(_card(
            card_type="agent_action",
            metadata={"needs_approval": True},
            body="c" * (FEED_RENDERED_BODY_LIMIT + 1),
        ))
        assert "Approve" in out and "Deny" in out
        assert ">Accept<" not in out and ">Reject<" not in out
        assert "Full command text — view before approving" in out

    def test_diff_pending_and_resolved_and_system(self):
        pending = render_feed_card(_card(card_type="diff", body="patch"))
        assert ">Review<" in pending and ">Accept<" in pending and ">Reject<" in pending
        resolved = render_feed_card(_card(card_type="diff", body="patch", accepted=True))
        assert ">Review<" in resolved and ">Accept<" not in resolved
        assert "ACCEPTED" in resolved
        system = render_feed_card(_card())
        assert "feed-card-actions" not in system

    def test_file_path_and_task_id_are_escaped(self):
        filed = render_feed_card(_card(
            card_type="file_created",
            file_path="<etc>/passwd",
            body="",
        ))
        assert "&lt;etc&gt;/passwd" in filed
        assert "<etc>" not in filed
        task = render_feed_card(_card(
            card_type="task",
            title="Do <b>",
            task_id="t<1>",
            body="",
        ))
        assert "Do &lt;b&gt;" in task
        assert "ID: t&lt;1&gt;" in task

    def test_open_diff_context_colors_plus_and_minus_only(self):
        snap = ConversationSnapshot(
            snapshot_type="diff",
            diff_text="--- a\n+++ b\n+added\n-removed\n context",
        )
        out = render_feed_card(_card(conversation_snapshot=snap), context_open=True)
        assert 'class="feed-diff-add">+added' in out
        assert 'class="feed-diff-del">-removed' in out
        assert "feed-diff-add\">+++ " not in out
        assert "feed-diff-del\">--- " not in out
        closed = render_feed_card(_card(conversation_snapshot=snap), context_open=False)
        assert "added" not in closed

    def test_conversation_context_escapes_message_text(self):
        snap = ConversationSnapshot(
            snapshot_type="conversation",
            messages=[SnapshotMessage(role="You", text="<b>hi</b>")],
            total_messages=3,
        )
        out = render_feed_card(_card(conversation_snapshot=snap), context_open=True)
        assert "&lt;b&gt;hi&lt;/b&gt;" in out
        assert "showing last 1 of 3" in out

    def test_empty_document_and_non_dict_metadata(self):
        doc = render_feed_document([])
        assert "No activity yet" in doc
        assert "Agent actions will appear here" in doc
        card = _card()
        card.metadata = "nope"  # bypass the dataclass default
        out = render_feed_card(card)
        assert "hello" in out
        assert "feed-card-actions" not in out

    def test_container_and_card_class_attributes_are_pinned(self):
        # CSS-pass guard: styles only, so the emitted class attributes and
        # element order must not move. Pins the DOM contract chunk 2 relies on.
        out = render_feed_card(_card(card_type="git_commit", body="x"))
        assert out.startswith('<article class="feed-card feed-card-git" ')
        assert 'data-card-id="card-1"' in out
        assert 'class="feed-card-header"' in out
        assert 'class="feed-card-title"' in out
        assert 'class="feed-card-footer"' in out
        assert 'class="feed-card-body feed-body-mono"' in out
        doc = render_feed_document([out])
        assert '<div id="feed">' in doc
        assert 'class="feed-empty"' in render_feed_document([])
        # Accent classes come from FeedCardData.css_class_for_type, unchanged.
        for card_type, css_class in (
            ("diff", "feed-card-diff"),
            ("agent_action", "feed-card-agent"),
            ("system", "feed-card-system"),
            ("audit_report", "feed-card-audit"),
        ):
            assert f"feed-card {css_class}" in render_feed_card(
                _card(card_type=card_type, body="b")
            )

    def test_two_fragments_keep_order(self):
        doc = render_feed_document([
            render_feed_card(_card(card_id="old", title="Old")),
            render_feed_card(_card(card_id="new", title="New")),
        ])
        assert doc.index("Old") < doc.index("New")
        assert 'id="feed"' in doc


class TestPizzazzPins:
    """String-level pins for the feed-card presentation contract (audit round).

    No browser: these pin the exact markup/CSS/script tokens the WebKit
    surface and the copy-feedback path depend on.
    """

    def test_copy_button_is_icon_svg_with_action_attributes(self):
        out = render_feed_card(_card())
        assert 'class="feed-copy-btn"' in out
        assert 'data-feed-action="copy"' in out
        assert 'data-feed-card="card-1"' in out
        assert 'aria-label="Copy"' in out
        assert 'title="Copy"' in out
        assert "<svg" in out
        assert ">Copy<" not in out  # icon-only: no visible word label

    def test_verdict_badges_are_pills_inside_inline_flex_rule(self):
        accepted = render_feed_card(_card(
            card_type="diff", body="patch", accepted=True
        ))
        rejected = render_feed_card(_card(
            card_type="diff", body="patch", accepted=False
        ))
        assert '<span class="feed-accepted-badge">ACCEPTED</span>' in accepted
        assert '<span class="feed-rejected-badge">REJECTED</span>' in rejected
        # The shared pill rule is inline-flex — a real pill, not a text label.
        start = FEED_CSS.index(".feed-accepted-badge, .feed-rejected-badge")
        block = FEED_CSS[start:FEED_CSS.index("}", start)]
        assert "inline-flex" in block

    def test_action_buttons_carry_tint_classes(self):
        out = render_feed_card(_card(card_type="diff", body="patch"))
        assert 'class="feed-btn-review"' in out
        assert 'class="feed-btn-accept"' in out
        assert 'class="feed-btn-reject"' in out

    def test_approval_card_carries_class(self):
        out = render_feed_card(_card(
            card_type="agent_action",
            metadata={"needs_approval": True},
        ))
        assert "feed-card-approval" in out

    def test_header_band_and_body_typography_css_pins(self):
        header_start = FEED_CSS.index(".feed-card-header {")
        header_block = FEED_CSS[header_start:FEED_CSS.index("}", header_start)]
        assert "linear-gradient" in header_block
        body_start = FEED_CSS.index(".feed-card-body {")
        body_block = FEED_CSS[body_start:FEED_CSS.index("}", body_start)]
        assert "font-size: 12.5px" in body_block

    def test_keyframes_are_opacity_only(self):
        start = FEED_CSS.index("@keyframes feed-card-in")
        to_start = FEED_CSS.index("to {", start)
        keyframes = FEED_CSS[start:FEED_CSS.index("}", to_start) + 1]
        assert "opacity: 0" in keyframes
        assert "opacity: 1" in keyframes
        assert "transform" not in keyframes  # never fights :hover

    def test_canvas_and_entrance_animation_css_pins(self):
        # SPEC-57 §2b: html canvas rule; the entrance animation moved off
        # .feed-card onto .feed-card-enter (name unchanged).
        assert "html { background: #1a1b26; height: 100%; }" in FEED_CSS
        card_start = FEED_CSS.index(".feed-card {")
        card_block = FEED_CSS[card_start:FEED_CSS.index("}", card_start)]
        assert "animation: feed-card-in" not in card_block
        enter_start = FEED_CSS.index(".feed-card-enter {")
        enter_block = FEED_CSS[enter_start:FEED_CSS.index("}", enter_start)]
        assert "animation: feed-card-in" in enter_block

    def test_script_gates_copied_state_on_bridge_after_post(self):
        script = FEED_SCRIPT
        # Copy feedback must only run when a live bridge received the message.
        assert 'if (bridge && btn.classList.contains("feed-copy-btn")) {' in script
        # The toggle must happen after the post() call ships the message.
        assert script.index("post({") < script.index('classList.add("copied")')
        assert "clearTimeout(btn._copyTimer)" in script
        assert 'btn._copyTimer = setTimeout(function () {' in script
        assert '}, 1200);' in script

    def test_full_document_contains_svg_and_keyframes(self):
        doc = render_feed_document([render_feed_card(_card())])
        assert "<svg" in doc
        assert "@keyframes feed-card-in" in doc
        assert FEED_CSS in doc


class _GLib:
    def idle_add(self, fn, *args, **kwargs):
        fn(*args)
        return 0


class _Surface:
    def __init__(self, ids):
        self.order = list(ids)
        self.removed = []
        self.revealed = []

    def card_ids(self):
        return list(self.order)

    def remove(self, card_id):
        self.order.remove(card_id)
        self.removed.append(card_id)

    def reveal(self, card_id):
        self.revealed.append(card_id)

    def toggle_context(self, card_id):
        self.revealed.append(("context", card_id))


class _Tab:
    def __init__(self, near=True):
        self.near = near
        self.upserts = []
        self.load_more = None

    def upsert_card(self, card):
        self.upserts.append(card.card_id)

    def is_near_bottom(self, slack=80):
        return self.near

    def set_html_load_more(self, remaining):
        self.load_more = remaining


def _handler():
    from ui.handlers.feed_handler import FeedHandler
    return FeedHandler(GLib=_GLib(), on_send_to_agent=lambda *_a: None)


class TestFeedActions:
    def test_approve_and_deny_dispatch(self):
        handler = _handler()
        seen = []
        handler._on_approve_exec = lambda cid, ok: seen.append((cid, ok))
        card = _card(card_type="agent_action", metadata={"needs_approval": True})
        handler._cards[card.card_id] = card
        handler.on_feed_action(card.card_id, "accept")
        handler.on_feed_action(card.card_id, "reject")
        assert seen == [("card-1", True), ("card-1", False)]

    def test_unknown_action_and_id_do_nothing(self):
        handler = _handler()
        seen = []
        handler._on_approve_exec = lambda cid, ok: seen.append(ok)
        handler._cards["card-1"] = _card()
        handler.on_feed_action("missing", "accept")
        handler.on_feed_action("card-1", "dance")
        assert seen == []

    def test_copy_and_reveal_use_body_or_title(self):
        handler = _handler()
        copied = []
        handler.handle_copy = lambda text: copied.append(text)
        surface = _Surface([])
        handler._feed_surface = surface
        empty = _card(card_id="e", body="", title="Just title")
        full = _card(card_id="f", body="body")
        handler._cards["e"] = empty
        handler._cards["f"] = full
        surface.order = ["f"]
        surface._cards = {"f": full}  # reveal checks the real surface; fake ignores
        handler.on_feed_action("e", "copy")
        handler.on_feed_action("f", "reveal")
        assert copied == ["Just title"]
        assert surface.revealed == ["f"]

    def test_non_file_accept_upserts_badge(self):
        handler = _handler()
        tab = _Tab()
        handler._html_feed = True
        handler._feed_tab = tab
        card = _card(card_type="system", body="note")
        handler._cards[card.card_id] = card
        handler.on_feed_action(card.card_id, "accept")
        assert card.accepted is True
        assert tab.upserts == ["card-1"]

    def test_review_toggles_when_there_is_no_widget(self):
        handler = _handler()
        surface = _Surface(["card-1"])
        handler._feed_surface = surface
        card = _card()
        handler._cards[card.card_id] = card
        handler.handle_review(card.card_id, None)
        handler.handle_review(card.card_id, None)
        assert surface.revealed == [("context", "card-1"), ("context", "card-1")]
        assert card.reviewed is True

    def test_evict_near_bottom_keeps_newest_and_drops_oldest(self, monkeypatch):
        handler = _handler()
        ids = [f"c{i}" for i in range(50)]
        for i, cid in enumerate(ids, start=1):
            card = _card(card_id=cid, body="b")
            card.seq_num = i
            handler._cards[cid] = card
        surface = _Surface(ids)
        tab = _Tab(near=True)
        handler._html_feed = True
        handler._feed_surface = surface
        handler._feed_tab = tab
        monkeypatch.setattr(handler, "_effective_live_window", lambda: 10)
        handler._evict_surplus_html_rows()
        remaining = set(surface.card_ids())
        assert len(remaining) == 40
        assert {f"c{i}" for i in range(10, 50)} == remaining
        assert surface.removed == [f"c{i}" for i in range(10)]
        assert tab.load_more == 10

    def test_surface_toggle_reveal_and_body_reset_without_a_page_load(self):
        from ui.views.feed_surface import FeedSurface
        surface = FeedSurface.__new__(FeedSurface)
        card = _card()
        surface._order = [card.card_id]
        surface._cards = {card.card_id: card}
        surface._html = {}
        surface._body = {card.card_id: card.body}
        surface._revealed = set()
        surface._context_open = set()
        surface._dirty = False
        surface._render_source = None
        surface._load_in_flight = True
        surface.toggle_context(card.card_id)
        surface.toggle_context(card.card_id)
        assert card.card_id not in surface._context_open
        surface.toggle_context(card.card_id)
        assert card.card_id in surface._context_open
        surface.reveal(card.card_id)
        assert card.card_id in surface._revealed
        changed = _card(body="replaced")
        surface.upsert(changed)
        assert card.card_id not in surface._revealed
        assert surface._body[card.card_id] == "replaced"

    def test_evict_when_scrolled_up_drops_nothing(self, monkeypatch):
        handler = _handler()
        ids = [f"c{i}" for i in range(5)]
        for i, cid in enumerate(ids, start=1):
            card = _card(card_id=cid)
            card.seq_num = i
            handler._cards[cid] = card
        surface = _Surface(ids)
        handler._html_feed = True
        handler._feed_surface = surface
        handler._feed_tab = _Tab(near=False)
        monkeypatch.setattr(handler, "_effective_live_window", lambda: 1)
        handler._evict_surplus_html_rows()
        assert surface.removed == []
        assert surface.card_ids() == ids


class TestHtmlProjectOpen:
    def test_open_paints_cards_without_gtk_remove_card(self, monkeypatch):
        """Project open on the HTML feed must not call FeedTab.remove_card.

        The GTK load-more sentinel is gone. set_html_load_more owns that bar.
        """
        import ui.handlers.feed_handler as fh

        class _SyncThreading:
            class Thread:
                def __init__(self, target=None, daemon=None):
                    self._target = target

                def start(self):
                    self._target()

        class _OpenTab:
            def __init__(self):
                self.cards = None
                self.load_more = None

            def set_html_cards(self, cards):
                self.cards = [c.card_id for c in cards]

            def set_html_load_more(self, remaining):
                self.load_more = remaining

            def schedule_smart_scroll_to_bottom(self):
                return None

            def is_near_bottom(self, slack=80):
                return True

        cards = [_card(card_id=f"c{i}", seq_num=i) for i in range(1, 17)]
        store = type("Store", (), {})()
        store.load_feed = lambda _path: list(cards)
        store.load_feed_prefs = lambda _path: {}
        store.FEED_WINDOW_DEFAULT = 2000
        handler = _handler()
        monkeypatch.setattr(fh, "threading", _SyncThreading)
        monkeypatch.setattr(fh, "feed_store", store)

        tab = _OpenTab()
        handler._html_feed = True
        handler._feed_tab = tab
        handler.on_project_opened("proj", "/tmp/proj")

        assert tab.cards == [f"c{i}" for i in range(2, 17)]
        assert tab.load_more == 1
