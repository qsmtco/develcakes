# utils/live_guard.py — SPEC-19 SP1 enforcement core.
#
# The outbound boundary for the live chat surface (SPEC-19 §3 E1+E2). Two
# mechanisms, both load-bearing:
#
#   E1 — COMPILED CONTENT FILTER (blanket block-all-remote)
#     A WebKit UserContentFilterStore-compiled filter attached via the view's
#     UserContentManager. ONE rule: {"trigger": {"url-filter": ".*"},
#     "action": {"type": "block"}}.
#
#     ★ PROBE-PINNED RULESET (SPEC-19 SP1 research, 2026-10-08, WebKit
#     2.52.6 / xvfb, realized view + local HTTP control server):
#       - The blanket `.*` block DOES block fetch / XMLHttpRequest / <img
#         subresource / <script src> (control server hit-count: 3 hits with no
#         filter, 0 hits with the filter) and WebSocket / EventSource /
#         sendBeacon (sendBeacon returned false; zero server hits).
#       - CRITICALLY: it does NOT block the surface's own `about:blank`
#         document load — `load_html(..., "about:blank")` still succeeds and
#         evaluate_javascript still runs (probe: `document.body.textContent`
#         returned the loaded text with the filter attached).
#       - So NO exemption rule / ignore-previous-rules ordering is needed: one
#         blanket rule suffices. (The phase instructions' research step asked
#         for exactly this determination; the working shape is pinned here.)
#     Probe artifact: .debug/SPEC19-decisive-probe.py.bak.
#
#   E2 — NAVIGATION LOCK (decide-policy)
#     Deny every navigation EXCEPT app-initiated loads of the surface's own
#     document. Probe-pinned: `load_html(doc, "about:blank")` fires
#     decide-policy with NavigationType OTHER and URI "about:blank"; every
#     other navigation (link click, redirect, page-initiated) is denied.
#
# No GTK widget subclassing here; WebKit is imported lazily so the module is
# unit-testable with fakes (the ruleset constant + decision logic are pure).

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from typing import Any

_logger = logging.getLogger(__name__)

# ── E1: the ruleset (probe-pinned; see header) ───────────────────────────
#
# EXACTLY ONE rule. The blanket `.*` block is proven (header) to block every
# remote channel while sparing about:blank — no exemption rule is required,
# and adding one would only add ordering fragility.
BLOCK_ALL_REMOTE_RULESET: list[dict[str, Any]] = [
    {"trigger": {"url-filter": ".*"}, "action": {"type": "block"}},
]

# E2 allowance: the ONLY navigation the guard permits is an app-initiated
# load of the surface's own document. Probe-pinned shape: NavigationType OTHER
# + an about: URI (load_html's about:blank). Everything else is denied.
_ALLOWED_NAV_URI_PREFIX = "about:"

# Filter identifier in the store (stable so a saved filter is reused).
_FILTER_IDENTIFIER = "develcakes-block-all-remote"

# Default store location (cache dir; 0700).
_DEFAULT_STORE_SUBDIR = os.path.join("develcakes", "content-filters")


def default_store_path() -> str:
    """The UserContentFilterStore directory (created 0700 by the store)."""
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, _DEFAULT_STORE_SUBDIR)


def _load_webkit():
    """Lazy WebKit import (keeps this module import-safe on WebKit-less boxes)."""
    import gi  # type: ignore[reportMissingImports]

    gi.require_version("WebKit", "6.0")
    from gi.repository import WebKit  # type: ignore[reportMissingImports]

    return WebKit


def _load_glib():
    from gi.repository import GLib  # type: ignore[reportMissingImports]

    return GLib


class LiveGuard:
    """Owns the content-filter + navigation-lock lifecycle for one WebView.

    Args:
        store_path: UserContentFilterStore dir (default: cache dir).
        on_navigation_denied: optional callback(uri: str) invoked when a
            navigation is denied (diagnostics).
    """

    def __init__(self, store_path: str | None = None,
                 on_navigation_denied: Callable[[str], None] | None = None) -> None:
        self._store_path = store_path or default_store_path()
        self._on_navigation_denied = on_navigation_denied
        self._filter: Any | None = None
        self._store: Any | None = None
        self._handler_id = 0
        self._view: Any | None = None
        self._filters_ready = False

    # ── E1: filter compile ───────────────────────────────────────────────

    def compile(self, callback: Callable[[Any], None] | None = None) -> None:
        """Compile (or load) the block-all-remote filter asynchronously.

        Idempotent: a second call while/after compiling is a no-op. On
        success `self._filter` is set and `callback(filter)` fires. On any
        failure the callback (if given) receives None — the caller decides
        (SP1: the surface must NOT go live without a filter).
        """
        if self._filter is not None:
            if callback is not None:
                callback(self._filter)
            return
        WebKit = _load_webkit()
        GLib = _load_glib()
        os.makedirs(self._store_path, mode=0o700, exist_ok=True)
        store = WebKit.UserContentFilterStore.new(self._store_path)
        self._store = store
        rules = GLib.Bytes.new(json.dumps(BLOCK_ALL_REMOTE_RULESET).encode("utf-8"))

        def _saved(src, result, _data=None):
            try:
                self._filter = store.save_finish(result)
            except Exception:
                _logger.exception("live_guard: content-filter compile failed")
                self._filter = None
            if callback is not None:
                callback(self._filter)

        try:
            store.save(_FILTER_IDENTIFIER, rules, None, _saved, None)
        except Exception:
            _logger.exception("live_guard: filter store.save raised")
            if callback is not None:
                callback(None)

    # ── E1+E2: attach ────────────────────────────────────────────────────

    def attach(self, view: Any) -> bool:
        """Attach the compiled filter + the navigation lock to `view`.

        Returns True when the filter is attached (the guard is active). The
        nav lock is connected regardless; the filter MUST be compiled first
        (compile() with a callback, then attach in that callback).
        """
        WebKit = _load_webkit()
        self._view = view
        if self._filter is None:
            _logger.error("live_guard: attach without a compiled filter — refusing")
            return False
        try:
            view.get_user_content_manager().add_filter(self._filter)
        except Exception:
            _logger.exception("live_guard: add_filter failed")
            return False
        try:
            self._handler_id = view.connect("decide-policy", self._on_decide_policy,
                                            WebKit)
        except Exception:
            _logger.exception("live_guard: decide-policy connect failed")
            return False
        return True

    def detach(self, view: Any | None = None) -> None:
        """Remove the filter + disconnect the handler (destroy hygiene)."""
        target = view if view is not None else self._view
        WebKit = None
        try:
            WebKit = _load_webkit()
        except Exception:  # noqa: BLE001
            WebKit = None
        if target is not None and self._filter is not None and WebKit is not None:
            try:
                target.get_user_content_manager().remove_filter(self._filter)
            except Exception:
                _logger.debug("live_guard: remove_filter failed", exc_info=True)
        if target is not None and self._handler_id:
            try:
                target.disconnect(self._handler_id)
            except (TypeError, ValueError):
                _logger.debug("live_guard: decide-policy already disconnected")
        self._handler_id = 0
        self._view = None

    # ── E2: the decision ─────────────────────────────────────────────────

    @staticmethod
    def is_allowed_navigation(nav_type: Any, uri: str | None, WebKit: Any) -> bool:
        """The E2 rule (pure — unit-tested with fakes).

        ALLOW only an app-initiated load of the surface's own document:
        navigation type OTHER (load_html, not a link/redirect/form) AND an
        `about:` URI. DENY everything else (links, redirects, page-initiated
        navigations, remote URIs).
        """
        if uri is None:
            return False
        if not uri.startswith(_ALLOWED_NAV_URI_PREFIX):
            return False
        other = getattr(getattr(WebKit, "NavigationType", None), "OTHER", None)
        return nav_type == other

    def _on_decide_policy(self, _view, decision, decision_type, WebKit) -> bool:
        """decide-policy handler: use (allow) the initial document load,
        ignore (deny) every other navigation."""
        try:
            nav_cls = getattr(WebKit, "PolicyDecisionType", None)
            if nav_cls is not None and decision_type != nav_cls.NAVIGATION_ACTION:
                # Non-navigation decisions (e.g. response policy) are left to
                # the default handler.
                return False
            action = decision.get_navigation_action()
            nav_type = action.get_navigation_type()
            request = action.get_request()
            uri = request.get_uri() if request is not None else None
            if self.is_allowed_navigation(nav_type, uri, WebKit):
                decision.use()  # the surface's own document load
                return True
            decision.ignore()  # everything else: denied
            if self._on_navigation_denied is not None:
                try:
                    self._on_navigation_denied(uri or "")
                except Exception:
                    _logger.debug("live_guard: on_navigation_denied raised", exc_info=True)
            return True
        except Exception:
            _logger.exception("live_guard: decide-policy handler failed — denying")
            try:
                decision.ignore()
            except Exception:
                _logger.debug("live_guard: ignore() failed in the failure path", exc_info=True)
            return True