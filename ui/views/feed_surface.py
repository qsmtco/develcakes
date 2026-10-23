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
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib

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


# SPEC-57 §2a: WebKit's default clear color is opaque white. Paint the
# canvas #1a1b26 before the first load_html; do not call again after a load.
_CANVAS = "#1a1b26"


def _apply_canvas_color(webview) -> None:
    """Draw #1a1b26 before the page paints.

    WebKit's default is opaque white. The call has to happen before the
    first load_html. set_background_color returns None. A later call does
    not refresh the accelerated-compositing clear color, so do not call
    this again after a load.
    """
    rgba = Gdk.RGBA()
    if not rgba.parse(_CANVAS):
        logger.error("canvas color %s did not parse", _CANVAS)
        return
    try:
        webview.set_background_color(rgba)
    except Exception:
        logger.exception("set_background_color failed")


# The patch script reuses the document's own near-bottom slack. FEED_SCRIPT's
# `nearBottom()` compares against 80px; this constant must stay in lockstep.
_NEAR_BOTTOM_PX = 80


def _ops_script(ops: list[dict]) -> str:
    """One JavaScript program that applies the DOM patch `ops`.

    Returns ``[out, outcome]`` where ``out`` is the array of resulting
    ``data-card-id`` strings (so the eval callback can commit ``_dom_ids``)
    and ``outcome`` is ``'followed'`` when the reader was near the bottom or
    ``'held'`` otherwise. Removal uses ``removeChild`` and replacement uses
    ``replaceChild`` (never ``.remove`` / ``.replaceWith``). Inserted
    articles get the ``feed-card-enter`` class; replacements do not.

    Nodes are found with ``find()``: it scans ``feed.querySelectorAll(
    'article[data-card-id]')`` and compares the DECODED ``getAttribute``
    value against the (JSON-escaped, decoded) op id. A CSS attribute selector
    is never built from a card id — a quote or ``]`` in an id would make
    ``querySelector`` throw. The prepend anchor is captured AFTER removals.

    Scroll: ``y`` and ``before`` are captured before mutating. Following
    readers (near bottom) land on the bottom. While NOT following, a prepend
    shifts the content above the viewport, so ``scrollTop`` is corrected by
    the ``scrollHeight`` delta; replace/append does not touch scroll when not
    following. The 80px slack matches FEED_SCRIPT.
    """
    lines = [
        "(function () {",
        "  var feed = document.getElementById('feed');",
        "  if (!feed) return [[], 'held'];",
        "  function find(id) {",
        "    var all = feed.querySelectorAll('article[data-card-id]');",
        "    for (var i = 0; i < all.length; i++) {",
        "      if (all[i].getAttribute('data-card-id') === id) return all[i];",
        "    }",
        "    return null;",
        "  }",
        "  var doc = document.documentElement;",
        "  var y = window.scrollY || doc.scrollTop || 0;",
        "  var viewport = window.innerHeight || doc.clientHeight || 0;",
        f"  var wasBottom = (doc.scrollHeight - viewport - y) <= {_NEAR_BOTTOM_PX};",
        "  var before = doc.scrollHeight;",
        "  var prepended = false;",
    ]
    for op in ops:
        if op.get("op") == "remove":
            lines.append(
                "  var el = find("
                + json.dumps(op["id"], ensure_ascii=True)
                + ");"
            )
            lines.append(
                "  if (el && el.parentNode === feed) feed.removeChild(el);"
            )
    lines.append("  var anchor = feed.firstChild;")
    for op in ops:
        kind = op.get("op")
        if kind not in ("append", "prepend", "replace"):
            continue
        lines.append("  var wrap = document.createElement('div');")
        lines.append(
            "  wrap.innerHTML = "
            + json.dumps(op["html"], ensure_ascii=True)
            + ";"
        )
        lines.append("  var node = wrap.firstChild;")
        if kind in ("append", "prepend"):
            lines.append(
                "  if (node) node.className = (node.className ? "
                "node.className + ' ' : '') + 'feed-card-enter';"
            )
        if kind == "append":
            lines.append("  if (node) feed.appendChild(node);")
        elif kind == "prepend":
            lines.append("  prepended = true;")
            lines.append("  if (node) feed.insertBefore(node, anchor);")
        else:
            lines.append(
                "  var old = find("
                + json.dumps(op["id"], ensure_ascii=True)
                + ");"
            )
            lines.append(
                "  if (old && old.parentNode === feed) "
                "feed.replaceChild(node, old);"
            )
    lines.extend([
        "  if (wasBottom) {",
        "    window.scrollTo(0, doc.scrollHeight);",
        "  } else if (prepended) {",
        "    var grew = doc.scrollHeight - before;",
        "    if (grew > 0) window.scrollTo(0, y + grew);",
        "  }",
        "  var out = []; var kids = feed.children;",
        (
            "  for (var i = 0; i < kids.length; i++) "
            "out.push(kids[i].getAttribute('data-card-id'));"
        ),
        "  return [out, wasBottom ? 'followed' : 'held'];",
        "})();",
    ])
    return "\n".join(lines)


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
        # Feed patch state (SPEC-57 §2f). `_dom_ready` / `_dom_ids` track what
        # the live document actually contains so `_flush` can patch it instead
        # of reloading. `_epoch` bumps on every `set_cards`; async callbacks
        # carry the epoch they were issued under and ignore newer generations.
        self._dom_ready = False
        self._dom_ids: list[str] = []
        self._dom_html: dict[str, str] = {}
        self._patch_in_flight = False
        self._epoch = 0
        self._load_epoch: int | None = None
        self._queued_reload = False
        self._last_ops: list | None = None
        self._patch_count = 0
        self._webview = WebKit.WebView()
        _apply_canvas_color(self._webview)
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
        """Replace the document. `cards` is oldest-first.

        A new document is a new reading context: reset the scroll state with
        it. Both fields are reported by the PREVIOUS document's FEED_SCRIPT,
        so carrying them over makes a switched-to project inherit the old
        project's pixel offset AND its follow-off state (it opens mid-page and
        its new cards land below the fold forever). A fresh project starts at
        the top and follows.

        SPEC-57 §2f: the epoch bumps FIRST so any async patch/load callback
        issued under the previous document is stale, and the in-flight gates
        are cleared so the next schedule takes a fresh full load.
        """
        self._epoch += 1
        self._patch_in_flight = False
        self._load_in_flight = False
        self._dom_ready = False
        self._dom_ids = []
        self._dom_html = {}
        self._queued_reload = False
        self._load_epoch = None
        self._order = []
        self._cards = {}
        self._html = {}
        self._body = {}
        self._revealed = set()
        self._context_open = set()
        self._doc_near_bottom = True
        self._doc_y = None
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
        if (
            self._render_source is not None
            or self._load_in_flight
            or self._patch_in_flight
        ):
            return
        self._render_source = GLib.idle_add(self._flush)

    def _flush(self) -> bool:
        self._render_source = None
        if self._load_in_flight or self._patch_in_flight:
            return False
        if self._queued_reload:
            self._queued_reload = False
            self._dom_ready = False
            self._dom_ids = []
            self._dom_html = {}
        # Patch path: the document is loaded and we know what it contains.
        if self._dom_ready and self._dom_ids and self._order:
            self._fragments()
            ops = self._plan_feed_ops()
            if ops == []:
                self._dirty = False
                return False
            if ops is not None:
                self._dirty = False
                self._patch_in_flight = True
                self._last_ops = ops
                self._patch_count += 1
                script = _ops_script(ops)
                epoch = self._epoch

                def _after(view, result, _data, _epoch=epoch):
                    self._on_patch_finished(view, result, _epoch)

                try:
                    self._webview.evaluate_javascript(
                        script, len(script.encode("utf-8")),
                        None, None, None, _after, None,
                    )
                except Exception:
                    logger.exception("feed patch eval failed")
                    self._recover_missing()
                return False
        # Full-document load. Follow decision comes from the DOCUMENT's own
        # scroll_state reports (see `_on_script_message`), never from a GTK
        # adjustment probe — the host ScrolledWindow does not scroll this
        # content at all.
        self._dirty = False
        self._scroll_after = bool(self._doc_near_bottom)
        self._load_epoch = self._epoch
        document = render_feed_document(self._fragments())
        self._load_in_flight = True
        try:
            self._webview.load_html(document, "about:blank")
        except Exception:
            logger.exception("feed load_html failed")
            self._load_in_flight = False
        return False

    def _plan_feed_ops(self) -> list[dict] | None:
        """Diff the live DOM against the desired ``_order``.

        Returns ``[]`` when the DOM already matches, a list of DOM patch ops
        (``remove`` / ``replace`` / ``prepend`` / ``append``) when the change
        is expressible as edge-only inserts, and ``None`` when the change
        needs a full-document reload (duplicate ids, reorder, a prepend+
        append mix, all-old-gone-plus-new, or a missing rendered fragment).
        """
        order = self._order
        dom = self._dom_ids
        if len(order) != len(set(order)):
            return None
        if any(cid not in self._html for cid in order):
            return None
        wanted = set(order)
        dom_set = set(dom)
        replacements = [
            cid
            for cid in order
            if cid in dom_set and self._html.get(cid) != self._dom_html.get(cid)
        ]
        if order == dom:
            return replacements if replacements else []
        if dom and wanted and not (dom_set & wanted):
            return None
        keep = [cid for cid in dom if cid in wanted]
        pos = {cid: i for i, cid in enumerate(order)}
        prev = -1
        for cid in keep:
            p = pos[cid]
            if p < prev:
                return None
            prev = p
        removals = [cid for cid in dom if cid not in wanted]
        additions = [cid for cid in order if cid not in dom_set]
        mode = None
        if additions and keep:
            first_keep = pos[keep[0]]
            last_keep = pos[keep[-1]]
            a_min = min(pos[a] for a in additions)
            a_max = max(pos[a] for a in additions)
            if a_min > last_keep:
                mode = "append"
            elif a_max < first_keep:
                mode = "prepend"
            else:
                return None
        elif additions and not keep:
            return None
        ops: list[dict] = []
        for cid in removals:
            ops.append({"op": "remove", "id": cid})
        for cid in replacements:
            ops.append({"op": "replace", "id": cid, "html": self._html[cid]})
        if mode == "prepend":
            for cid in additions:
                ops.append({"op": "prepend", "id": cid, "html": self._html[cid]})
        elif mode == "append":
            for cid in additions:
                ops.append({"op": "append", "id": cid, "html": self._html[cid]})
        return ops

    def _recover_missing(self) -> None:
        """The DOM no longer reflects our model — fall back to a full load.

        Clears the patch gate and the DOM model, marks the surface dirty, and
        schedules. The next flush takes the load branch (``_dom_ready`` is
        False again). There is deliberately NO patch retry loop.
        """
        self._patch_in_flight = False
        self._dom_ready = False
        self._dom_ids = []
        self._dom_html = {}
        self._queued_reload = True
        self._dirty = True
        self._schedule()

    def _on_patch_finished(self, view, result, epoch) -> None:
        """Eval callback for a patch. `epoch` is the generation issued at eval.

        A stale epoch (a newer ``set_cards`` or patch superseded this one) is
        ignored: it must NOT write the DOM model and must NOT clear a newer
        ``_patch_in_flight``. A missing / unparseable id list means the DOM is
        gone or inconsistent — recover with a full load.
        """
        if epoch != self._epoch:
            return
        self._patch_in_flight = False
        try:
            raw = result.to_json(0) if hasattr(result, "to_json") else None
            payload = json.loads(raw) if isinstance(raw, str) else None
        except Exception:
            logger.exception("feed patch result was not JSON")
            payload = None
        if (
            not isinstance(payload, list)
            or len(payload) != 2
            or not isinstance(payload[0], list)
            or not all(isinstance(i, str) for i in payload[0])
        ):
            self._recover_missing()
            return
        ids, outcome = payload
        if not ids and self._order:
            self._recover_missing()
            return
        logger.debug("feed patch applied: %s", outcome)
        self._dom_ids = ids
        self._dom_html = {
            cid: self._html.get(cid, self._dom_html.get(cid, ""))
            for cid in ids
        }
        if self._dirty:
            self._schedule()

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
        # Epoch check: a set_cards that landed while this document loaded made
        # it stale. Do not adopt its DOM and do not restore scroll on it —
        # schedule a fresh load instead.
        if self._epoch != self._load_epoch:
            self._dom_ready = False
            self._dom_ids = []
            self._dom_html = {}
            self._dirty = True
            self._schedule()
            return
        self._dom_ready = True
        self._dom_ids = list(self._order)
        self._dom_html = {cid: self._html.get(cid, "") for cid in self._order}
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
        self._dom_ready = False
        self._dom_ids = []
        self._dom_html = {}
        if self._dirty:
            self._schedule()
        return False

    def _on_web_process_terminated(self, view, reason) -> None:
        logger.error("feed web process terminated: %s", reason)
        self._load_in_flight = False
        self._dom_ready = False
        self._dom_ids = []
        self._dom_html = {}
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
