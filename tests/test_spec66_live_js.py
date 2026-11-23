# tests/test_spec66_live_js.py — SPEC-66 §2.11 / §11.6: LiveGuard fail-closed.
#
# R-15 row node: test_attach_false_leaves_js_off
#
# XVFB runner (real WebKit widgets). Two surfaces are covered because the app
# has TWO JS-enabled WebViews:
#   * ui/views/chat_surface.py  — `_ensure_webview` + `_on_live_guard_compiled`
#   * ui/views/feed_surface.py  — `FeedSurface.__init__` + its own compiled
#                                 callback (§11.6: it had NO guard at all)
#
# The contract, identical on both:
#   compile fails (None)      → JS OFF
#   attach returns False      → JS OFF
#   attach returns True       → JS ON, and the filter really is attached
# Nothing enables JS except a successful attach, so there is no window in
# which a document runs script without the enforcement boundary.

from __future__ import annotations

import pytest

from utils.live_guard import LiveGuard

GTK_HEADLESS_SAFE = False  # real widgets: xvfb row


def _js(webview) -> bool:
    return webview.get_settings().get_enable_javascript()


class _StubFilter:
    """Stands in for the compiled content filter."""


class TestChatSurfaceFailClosed:
    """ChatSurface: the transcript must never run script without the guard."""

    @staticmethod
    def _surface(monkeypatch, *, live: bool = True, attach_result: bool | None):
        """Build a ChatSurface whose guard compiles to a stub filter and whose
        attach returns `attach_result` (None = compile FAILS)."""
        from ui.views import chat_surface as cs_module

        monkeypatch.setattr(cs_module, "_live_js_enabled", lambda: live)

        def _fake_compile(self, callback):
            if attach_result is None:
                callback(None)          # compile failed
            else:
                callback(_StubFilter())

        def _fake_attach(self, view):
            return attach_result

        monkeypatch.setattr(LiveGuard, "compile", _fake_compile, raising=True)
        monkeypatch.setattr(LiveGuard, "attach", _fake_attach, raising=True)

        surface = cs_module.ChatSurface()
        surface._ensure_webview()
        return surface

    def test_attach_false_leaves_js_off(self, monkeypatch):
        """R-15 NODE: attach returning False must leave JS OFF."""
        surface = self._surface(monkeypatch, attach_result=False)
        try:
            assert _js(surface._webview) is False, (
                "JS stayed enabled after attach() returned False — the "
                "transcript is running unbounded"
            )
        finally:
            surface.destroy()

    def test_compile_failure_leaves_js_off(self, monkeypatch):
        """No filter at all: JS must never have been enabled."""
        surface = self._surface(monkeypatch, attach_result=None)
        try:
            assert _js(surface._webview) is False, (
                "JS is enabled on a surface whose content filter never compiled"
            )
        finally:
            surface.destroy()

    def test_attach_true_enables_js_and_keeps_the_guard(self, monkeypatch):
        """Positive control: with a real attach the tier goes live — otherwise
        the fail-closed tests would pass on a permanently dead surface."""
        surface = self._surface(monkeypatch, attach_result=True)
        try:
            assert _js(surface._webview) is True, (
                "JS stayed off after a successful attach — the live tier is dead"
            )
            assert surface._live_guard is not None, (
                "the guard was dropped even though it attached"
            )
        finally:
            surface.destroy()

    def test_kill_switch_keeps_js_off_without_a_guard(self, monkeypatch):
        """The flag-OFF path: no guard is built and JS stays off."""
        surface = self._surface(monkeypatch, live=False, attach_result=True)
        try:
            assert _js(surface._webview) is False
            assert surface._live_guard is None
        finally:
            surface.destroy()

    def test_guard_failure_still_renders_new_messages(self, monkeypatch):
        """SPEC-66 W3-audit HIGH #2: a guard failure must degrade to the
        FULL-DOCUMENT path, never keep injecting into a JS-off view.

        Before the fix the surface kept `_live_js` True after a compile
        failure, so `_schedule_render` chose `_inject_new_rows` →
        `evaluate_javascript` (a no-op with JS off) and the transcript stayed
        blank while the surface reported itself idle (audit probe:
        `_document_cache_len=0`, empty DOM readback). Inverted here: the same
        append must reach `load_html` with the message in the document.
        """
        surface = self._surface(monkeypatch, attach_result=None)
        try:
            assert surface._live_js is False, (
                "the effective live state is still ON after the guard failed"
            )
            assert surface.is_live_enabled() is False, (
                "is_live_enabled() lies while JS is off — the render handler "
                "would keep emitting live sections that cannot be resurrected"
            )
            loaded: list[str] = []
            monkeypatch.setattr(surface, "_load_html", loaded.append)
            # The scroll read is the documented test seam; answer it locally so
            # this test does not depend on the async WebKit callback.
            monkeypatch.setattr(
                surface, "_document_eval",
                lambda _script, cb: cb('{"atBottom": true, "y": 0}'),
            )

            surface.append_message("user", "<p>visible after guard failure</p>")
            surface._drain_renders()

            assert loaded, (
                "no full-document load happened — the surface kept the "
                "incremental injection path into a JS-off view (blank chat)"
            )
            assert "visible after guard failure" in loaded[-1], (
                f"the message never reached the document: {loaded[-1]!r}"
            )
        finally:
            surface.destroy()



class TestFeedSurfaceFailClosed:
    """FeedSurface (§11.6): the SECOND JS surface, previously unguarded."""

    @staticmethod
    def _surface(monkeypatch, *, attach_result: bool | None):
        from ui.views import feed_surface as fs_module

        def _fake_compile(self, callback):
            if attach_result is None:
                callback(None)
            else:
                callback(_StubFilter())

        def _fake_attach(self, view):
            return attach_result

        monkeypatch.setattr(LiveGuard, "compile", _fake_compile, raising=True)
        monkeypatch.setattr(LiveGuard, "attach", _fake_attach, raising=True)

        return fs_module.FeedSurface(
            lambda action, card_id: None,
            is_near_bottom=None,
            on_load_finished=None,
        )

    def test_attach_false_leaves_js_off(self, monkeypatch):
        """R-15 NODE (feed half): attach False ⇒ feed JS OFF."""
        surface = self._surface(monkeypatch, attach_result=False)
        assert _js(surface.webview) is False, (
            "the feed document is running script without the guard — the "
            "pre-fix §11.6 hole"
        )

    def test_compile_failure_leaves_js_off(self, monkeypatch):
        surface = self._surface(monkeypatch, attach_result=None)
        assert _js(surface.webview) is False

    def test_attach_true_enables_js(self, monkeypatch):
        """FEED_SCRIPT must still work when the guard is active — the feed
        needs JS for patching and scroll restore."""
        surface = self._surface(monkeypatch, attach_result=True)
        assert _js(surface.webview) is True, (
            "feed JS stayed off after a successful attach — patching and "
            "scroll restore are dead"
        )
        assert surface._live_guard is not None
