# ui/views/feed_surface.py — WebKit document of feed cards (SPEC-FEED-HTML-SURFACE).
#
# Not a ScrolledWindow. FeedTab owns the scroll and sets this webview as
# its child. Card text is escaped in render/feed_html.py. The only page
# script posts button clicks; it does not read card bodies.
#
# Probe 2026-10-17 (WebKit 6.0, xvfb-run + aa-exec -p develcakes-python):
#   mgr.register_script_message_handler("feed") -> True
#   signal "script-message-received::feed" callback (manager, JSC Value)
#   value.to_json(0) -> '{"action":"...","card_id":"..."}'
#   page: window.webkit.messageHandlers.feed.postMessage({action, card_id})
# Connecting the undetailed "script-message-received" as well delivers the
# same message twice. Connect the detailed signal only.
# load-failed is the failure signal (no LoadEvent.FAILED). A web-process
# crash fires web-process-terminated and neither load-failed nor FINISHED.

from __future__ import annotations

import json
import logging

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib

from render.feed_html import render_feed_card, render_feed_document
from utils.config import get_env

logger = logging.getLogger(__name__)

if get_env("NO_WEBKIT"):
    WebKit = None
else:
    try:
        gi.require_version("WebKit", "6.0")
        from gi.repository import WebKit
    except (ValueError, ImportError):
        WebKit = None


# Document-scroll scripts. The page owns scrolling (the host ScrolledWindow has
# PolicyType.NEVER vertical), so Python never reads a GTK adjustment.
_BOTTOM_SCRIPT = "window.scrollTo(0, document.documentElement.scrollHeight);"


def _restore_script(y: float) -> str:
    return f"window.scrollTo(0, {float(y)!r});"


def create_feed_surface(on_action, *, is_near_bottom=None, on_load_finished=None):
    """A feed document, or None when WebKit is off or the click channel fails.

    `is_near_bottom` / `on_load_finished` are the pre-document-scroll hooks.
    They are accepted for call compatibility and are NOT called: follow state
    now comes from the document's own scroll_state messages.
    """
    if WebKit is None:
        return None
    try:
        return FeedSurface(
            on_action,
            is_near_bottom=is_near_bottom,
            on_load_finished=on_load_finished,
        )
    except Exception:
        logger.exception("feed surface unavailable — staying on the GTK card list")
        return None


class FeedSurface:
    """One WebKit document of feed cards. The handler owns the FeedCardData."""

    def __init__(self, on_action, *, is_near_bottom, on_load_finished) -> None:
        self._on_action = on_action
        self._is_near_bottom = is_near_bottom
        self._on_load_finished = on_load_finished
        self._order: list[str] = []
        self._cards: dict = {}
        self._html: dict[str, str] = {}
        self._body: dict[str, str] = {}
        self._revealed: set[str] = set()
        self._context_open: set[str] = set()
        self._dirty = False
        self._render_source = None
        self._load_in_flight = False
        # Document-scroll state, reported by FEED_SCRIPT's scroll listener
        # (see `_on_script_message`). `_doc_near_bottom` is the reader's follow
        # intent; `_doc_y` is the last known document offset, restored after a
        # full-document reload. Python NEVER reads a GTK adjustment for these.
        self._doc_near_bottom = True
        self._doc_y: float | None = None
        self._scroll_after = False
        self._webview = WebKit.WebView()
        # The document scrolls ITSELF: the host ScrolledWindow runs with
        # PolicyType.NEVER vertical (feed_tab.attach_html_surface) and the
        # webview fills the viewport, so its own scrollbars move the page.
        self._webview.set_vexpand(True)
        self._webview.set_hexpand(True)
        self._webview.get_settings().set_enable_javascript(True)
        manager = self._webview.get_user_content_manager()
        # Signature recorded from the probe above. A False return means the
        # name was refused — raise so create_feed_surface keeps the GTK list.
        if not manager.register_script_message_handler("feed"):
            raise RuntimeError("register_script_message_handler('feed') returned False")
        manager.connect("script-message-received::feed", self._on_script_message)
        self._webview.connect("load-changed", self._on_load_changed)
        self._webview.connect("load-failed", self._on_load_failed)
        self._webview.connect("web-process-terminated", self._on_web_process_terminated)

    @property
    def webview(self):
        return self._webview

    def card_ids(self) -> list[str]:
        return list(self._order)

    def set_cards(self, cards: list) -> None:
        """Replace the document. `cards` is oldest-first."""
        self._order = []
        self._cards = {}
        self._html = {}
        self._body = {}
        self._revealed = set()
        self._context_open = set()
        for card in cards:
            cid = card.card_id or ""
            if not cid:
                continue
            self._order.append(cid)
            self._cards[cid] = card
            self._body[cid] = card.body if isinstance(card.body, str) else ""
        self._schedule()

    def upsert(self, card, *, prepend: bool = False) -> None:
        cid = card.card_id or ""
        if not cid:
            return
        body = card.body if isinstance(card.body, str) else ""
        previous = self._body.get(cid)
        if previous is not None and previous != body:
            self._revealed.discard(cid)
        self._cards[cid] = card
        self._body[cid] = body
        if cid not in self._order:
            if prepend:
                self._order.insert(0, cid)
            else:
                self._order.append(cid)
        self._schedule()

    def remove(self, card_id: str) -> None:
        if card_id not in self._order and card_id not in self._cards:
            return
        self._order = [cid for cid in self._order if cid != card_id]
        self._cards.pop(card_id, None)
        self._html.pop(card_id, None)
        self._body.pop(card_id, None)
        self._revealed.discard(card_id)
        self._context_open.discard(card_id)
        self._schedule()

    def reveal(self, card_id: str) -> None:
        if card_id not in self._cards:
            return
        self._revealed.add(card_id)
        self._schedule()

    def toggle_context(self, card_id: str) -> None:
        if card_id not in self._cards:
            return
        if card_id in self._context_open:
            self._context_open.discard(card_id)
        else:
            self._context_open.add(card_id)
        self._schedule()

    def _fragments(self) -> list[str]:
        parts: list[str] = []
        for cid in self._order:
            card = self._cards.get(cid)
            if card is None:
                continue
            fragment = render_feed_card(
                card,
                revealed=cid in self._revealed,
                context_open=cid in self._context_open,
            )
            self._html[cid] = fragment
            parts.append(fragment)
        return parts

    def _schedule(self) -> None:
        self._dirty = True
        if self._render_source is not None or self._load_in_flight:
            return
        self._render_source = GLib.idle_add(self._flush)

    def _flush(self) -> bool:
        self._render_source = None
        if self._load_in_flight:
            return False
        self._dirty = False
        # Follow decision comes from the DOCUMENT's own scroll_state reports
        # (see `_on_script_message`), never from a GTK adjustment probe — the
        # host ScrolledWindow does not scroll this content at all.
        self._scroll_after = bool(self._doc_near_bottom)
        document = render_feed_document(self._fragments())
        self._load_in_flight = True
        try:
            self._webview.load_html(document, "about:blank")
        except Exception:
            logger.exception("feed load_html failed")
            self._load_in_flight = False
        return False

    def _eval_document_scroll(self, script: str) -> None:
        """Fire-and-forget document scroll in the page's own world.

        The page owns scrolling (host ScrolledWindow is PolicyType.NEVER
        vertically); Python only asks the document to move. The callback is a
        no-op — a failed eval must never break the render loop.
        """
        view = self._webview
        if view is None:
            return

        def _done(*_args) -> None:
            return None

        try:
            view.evaluate_javascript(
                script, len(script.encode("utf-8")), None, None, None, _done, None,
            )
        except Exception:
            logger.exception("feed document scroll eval failed")

    def _on_load_changed(self, view, event) -> None:
        if WebKit is None or event != WebKit.LoadEvent.FINISHED:
            return
        self._load_in_flight = False
        follow = self._scroll_after
        self._scroll_after = False
        if follow:
            # Reader was following: land on the newest card.
            self._eval_document_scroll(_BOTTOM_SCRIPT)
        elif self._doc_y is not None:
            # Reader was reading older cards: put the document back.
            self._eval_document_scroll(_restore_script(self._doc_y))
        if self._dirty:
            self._schedule()

    def _on_load_failed(self, view, event, uri, error) -> bool:
        logger.error("feed document load failed: %s", error)
        self._load_in_flight = False
        if self._dirty:
            self._schedule()
        return False

    def _on_web_process_terminated(self, view, reason) -> None:
        logger.error("feed web process terminated: %s", reason)
        self._load_in_flight = False
        if self._dirty:
            self._schedule()

    def _on_script_message(self, manager, value) -> None:
        try:
            raw = value.to_json(0)
            data = json.loads(raw) if isinstance(raw, str) else None
        except Exception:
            logger.exception("feed click payload was not JSON")
            return
        if not isinstance(data, dict):
            return
        action = data.get("action")
        if action == "scroll_state":
            self._on_scroll_state(data)
            return
        card_id = data.get("card_id")
        if not isinstance(action, str) or not isinstance(card_id, str):
            return
        try:
            self._on_action(card_id, action)
        except Exception:
            logger.exception("feed action %s failed", action)

    def _on_scroll_state(self, data: dict) -> None:
        """Record the document's own scroll report (FEED_SCRIPT).

        The page is the only thing that knows where the reader is: the host
        ScrolledWindow never scrolls this content. `near_bottom` is the follow
        intent used by `_flush`; `y` is restored after a full-document reload.
        """
        near_bottom = data.get("near_bottom")
        if isinstance(near_bottom, bool):
            self._doc_near_bottom = near_bottom
        y = data.get("y")
        if isinstance(y, (int, float)) and not isinstance(y, bool):
            self._doc_y = float(y)
