# tests/test_spec58_round4_fixes.py
# SPEC-58 round-4 fix round — regression pins for the two findings.
#
# Finding 1 — ui/handlers/chat_render_handler.py: _mount_misses (the
#   consecutive-None-getter miss counter, FIX 11) is now cleared in
#   lockstep with the other lifecycle dicts on BOTH surface-teardown
#   paths: close_session, and the _mounted_box_keys fan-pop loop of
#   pop_tombstones_for_box. Pre-fix, misses accrued against a dead tab
#   survived close/reopen, so a fresh surface hit _MOUNT_MISS_LIMIT (3)
#   sooner and a render was dropped.
#
# Finding 2 — the _get_runtime raise risk (RuntimeError "No provider
#   configured — ..." / "No API key configured for provider ...", raised
#   BEFORE AgentRuntime construction at agent_runtime_handler.py
#   :907/:910) escaping GTK callbacks: chat_handler._send_local and
#   forward_handler.forward_to_agent (both branches) now wrap the
#   send_to_special_agent call in try/except Exception — the
#   review_handler._send_isolated idiom — logging a WARNING and
#   surfacing the error through the existing "System" bubble mechanism
#   instead of crashing the callback / silently vanishing the message.
#
# Run under xvfb like the rest of the suite:
#   xvfb-run -a .venv/bin/python -m pytest tests/test_spec58_round4_fixes.py -x -q
#
# Headless-safe: no GTK widget is constructed anywhere in this module — the
# handler tests drive fake chat boxes / spy surfaces / MagicMock boundaries
# only (the imported handler modules pull in the Gtk/WebKit NAMESPACES, which
# import fine without a display; only widget construction segfaults). Opt out
# of the conftest display guard (GTK_HEADLESS_SAFE, same marker as
# tests/test_perf_feed.py) so these pins run in the headless battery too.
GTK_HEADLESS_SAFE = True

import logging
from unittest.mock import MagicMock

from ui.handlers.chat_handler import ChatHandler
from ui.handlers.chat_render_handler import ChatRenderHandler


# ── Fakes (boundary spies — no WebKit, no real widget tree) ─────────────────


class SpySurface:
    """GTK-free stand-in for the chat surface — just the attributes the
    handler's mount/lifecycle code touches (same shape as the SpySurface
    in tests/test_chat_render_handler.py)."""

    def __init__(self):
        self._parent = None
        self.destroyed = False

    def get_parent(self):
        return self._parent

    def set_parent(self, parent):
        self._parent = parent

    def unparent(self):
        self._parent = None

    def destroy(self):
        self.destroyed = True

    def welcome(self, *a, **k):
        return None

    def append_message(self, *a, **k):
        pass


class FakeChatBox:
    """Plain chat-box fake — append() is the production API."""

    def __init__(self):
        self.bubbles = []

    def append(self, widget):
        self.bubbles.append(widget)


class FakeRenderHandler:
    """ChatRenderHandler spy — records render_sync role/text/session_key.

    R1 contract (chat_render_handler.py :849-851): render_sync returns
    None ALWAYS — the surface owns the widget tree, callers never get a
    bubble to append. (This fake used to return a truthy object(), which
    kept the dead `if bubble is not None: chat_box.append(...)` guards
    looking alive in tests and masked Debugger BUG#1 — the error surface
    orphaned under the agent key on project-tab sends.)"""

    def __init__(self):
        self.calls = []
        # The handler reads this attribute directly on the forwarded-bubble
        # path (the documented "may be None on first call" edge case).
        self._on_forward_message = None

    def render_sync(self, role, text, session_key, **kwargs):
        self.calls.append(
            {"role": role, "text": text, "session_key": session_key})
        return None  # R1: ALWAYS None — the render call IS the surface


class FakeMainContent:
    """MainContent spy — get_chat_box() (the current tab's box) is what
    the chat error path reads; get_current_session_key() is what
    _show_send_error renders under (the CURRENTLY VISIBLE tab's key,
    read at callback run time)."""

    def __init__(self, chat_box, current_session_key=None):
        self._chat_box = chat_box
        self._current_session_key = current_session_key

    def get_chat_box(self, page_index=None):
        return self._chat_box

    def get_current_session_key(self):
        return self._current_session_key


class RaisingReceiver:
    """AgentRuntimeHandler spy that raises the SPEC-58 shape: the
    _get_runtime guard fires before AgentRuntime construction."""

    def __init__(self, exc):
        self._exc = exc
        self.calls = []

    def send_to_special_agent(self, session_key, text, reply_target=None):
        self.calls.append((session_key, text, reply_target))
        raise self._exc


# ── Finding 1: _mount_misses cleared in teardown lockstep ───────────────────


class TestMountMissesTeardownLockstep:
    """SPEC-58 r4 Finding 1 — the miss counter dies with the surface, on
    BOTH teardown paths, even when it was pre-seeded."""

    def test_close_session_clears_pre_seeded_mount_misses(self):
        handler = ChatRenderHandler()
        handler._mount_misses["project:alpha"] = 3   # pre-seeded: at the cap
        handler._mount_misses["project:beta"] = 1    # unrelated key survives
        handler.close_session("project:alpha")
        assert "project:alpha" not in handler._mount_misses
        assert handler._mount_misses == {"project:beta": 1}

    def test_pop_tombstones_fan_pop_clears_victim_mount_misses(self):
        handler = ChatRenderHandler()
        # r3 FIX 3 tombstone bookkeeping: agent:sk died MOUNTED in the
        # project:alpha box; its miss counter is pre-seeded past the cap.
        handler._closed_sessions["agent:sk"] = True
        handler._mounted_box_keys["agent:sk"] = "project:alpha"
        handler._mount_misses["agent:sk"] = 3
        handler.pop_tombstones_for_box("project:alpha")
        # THE FIX: the fan-pop clears _mount_misses in lockstep with the
        # tombstone pops above it.
        assert "agent:sk" not in handler._mount_misses
        assert "agent:sk" not in handler._closed_sessions
        assert "agent:sk" not in handler._mounted_box_keys

    def test_pop_tombstones_leaves_other_boxes_victims_alone(self):
        handler = ChatRenderHandler()
        handler._closed_sessions["agent:other"] = True
        handler._mounted_box_keys["agent:other"] = "project:other"
        handler._mount_misses["agent:other"] = 2
        handler.pop_tombstones_for_box("project:alpha")
        assert handler._mount_misses.get("agent:other") == 2

    def test_close_then_reopen_budget_is_full_again(self):
        """End-to-end shape of the reported effect: 2 misses accrued
        against a dying tab, close + tombstone-pop — the fresh surface
        must get the FULL retry budget back (2 more misses ≠ eviction),
        not inherit the dead one's count."""
        boxes = {}
        handler = ChatRenderHandler()
        handler.set_chat_container_getter(lambda sk: boxes.get(sk))
        s = SpySurface()
        handler._surfaces["agent:sk"] = s  # pre-seeded: no factory/WebView
        handler._surface_for("agent:sk")   # miss 1 (no box for the key)
        handler._surface_for("agent:sk")   # miss 2
        assert handler._mount_misses["agent:sk"] == 2
        boxes["project:alpha"] = object()  # the box the victim died in
        handler._mounted_box_keys["agent:sk"] = "project:alpha"
        handler.close_session("project:alpha")
        handler.pop_tombstones_for_box("project:alpha")
        assert "agent:sk" not in handler._mount_misses
        # Reopen: the miss counter counts from ZERO — two more misses are
        # still under _MOUNT_MISS_LIMIT (pre-fix the fresh surface would
        # have been evicted on the very next miss).
        handler._surface_for("agent:sk")
        handler._surface_for("agent:sk")
        assert handler._mount_misses["agent:sk"] == 2
        assert handler._surfaces.get("agent:sk") is s  # not evicted


# ── Finding 2: _get_runtime raises must not escape GTK callbacks ────────────


class TestSendLocalRuntimeErrorSurfaced:
    """chat_handler._send_local — the receiver's RuntimeError (no provider
    configured) must not propagate out of the _dispatch'd callback, and
    the failure must be surfaced via the existing error-bubble path (the
    user's message must not vanish silently — the "You" bubble is already
    rendered by the time this runs)."""

    def test_send_local_does_not_propagate_and_surfaces_error(self, caplog):
        chat_box = FakeChatBox()
        render = FakeRenderHandler()
        handler = ChatHandler(FakeMainContent(chat_box), None, None)
        # GLib_module=None → _dispatch runs the callback inline
        handler._chat_render_handler = render
        receiver = RaisingReceiver(
            RuntimeError("No provider configured — set an API key."))
        handler.set_agent_runtime_handler(receiver)

        with caplog.at_level(logging.WARNING, logger="ui.handlers.chat_handler"):
            # No raise above is the primary assertion — pre-fix this
            # RuntimeError escaped into the GTK callback.
            handler._send_local("agent:main", "hello", reply_target="agent:main")

        assert receiver.calls == [("agent:main", "hello", "agent:main")]
        assert "Send failed" in caplog.text
        # Surfaced via the error path: a "System" render carrying the
        # RuntimeError text, under the send target (the current-tab key is
        # None here, so `current_key or session_key` falls back to it).
        assert len(render.calls) == 1
        assert render.calls[0]["role"] == "System"
        assert "No provider configured" in render.calls[0]["text"]
        assert render.calls[0]["session_key"] == "agent:main"
        # R1 (render_sync returns None ALWAYS): the render call IS the
        # surface — nothing is ever appended to the box. Pin the dead
        # append stays dead (Debugger BUG#1: the append was the "surface").
        assert chat_box.bubbles == []

    def test_send_local_survives_even_if_bubble_render_fails(self, caplog):
        """Best-effort guard: the bubble render itself blowing up must not
        take the callback down either."""

        class _ExplodingRender(FakeRenderHandler):
            def render_sync(self, *a, **k):
                raise RuntimeError("render exploded")

        handler = ChatHandler(FakeMainContent(FakeChatBox()), None, None)
        handler._chat_render_handler = _ExplodingRender()
        handler.set_agent_runtime_handler(
            RaisingReceiver(RuntimeError("No API key configured for provider x.")))
        with caplog.at_level(logging.WARNING, logger="ui.handlers.chat_handler"):
            handler._send_local("agent:main", "hello")  # must not raise
        assert "Send failed" in caplog.text
        assert "Could not surface" in caplog.text


class TestSendErrorTargetsCurrentTab:
    """Debugger BUG#1 (SPEC-58 r4, adjudicated): _show_send_error rendered
    the System surface under the AGENT target key. On project-tab sends
    that key orphans under a tab that does not exist — invisible failure,
    and the mount-miss counter evicts the orphan at the 3rd miss. The
    render key is now the CURRENTLY VISIBLE tab's key, read inside the
    callback at run time (the user can switch tabs between the dispatch
    and the error callback)."""

    def test_error_renders_under_current_tab_not_send_target(self, caplog):
        """THE PIN: user is looking at project:alpha when a send to
        agent:main fails → the render carries project:alpha."""
        render = FakeRenderHandler()
        handler = ChatHandler(
            FakeMainContent(FakeChatBox(), current_session_key="project:alpha"),
            None, None)
        handler._chat_render_handler = render
        handler.set_agent_runtime_handler(
            RaisingReceiver(RuntimeError("No provider configured.")))
        with caplog.at_level(logging.WARNING, logger="ui.handlers.chat_handler"):
            handler._send_local("agent:main", "hello")
        assert len(render.calls) == 1
        assert render.calls[0]["role"] == "System"
        # The CURRENT tab key, NOT the send target.
        assert render.calls[0]["session_key"] == "project:alpha"

    def test_current_key_none_falls_back_to_send_target(self, caplog):
        """No focused tab (or an unreadable one) at callback time → the
        send target key is the honest fallback."""
        render = FakeRenderHandler()
        handler = ChatHandler(
            FakeMainContent(FakeChatBox(), current_session_key=None),
            None, None)
        handler._chat_render_handler = render
        handler.set_agent_runtime_handler(
            RaisingReceiver(RuntimeError("No provider configured.")))
        with caplog.at_level(logging.WARNING, logger="ui.handlers.chat_handler"):
            handler._send_local("agent:main", "hello")
        assert len(render.calls) == 1
        assert render.calls[0]["session_key"] == "agent:main"

    def test_error_renders_even_without_a_chat_box(self, caplog):
        """No box for the current tab must not skip the surface — under
        R1 the render does not need one (the surface owns the widget
        tree); the old `get_chat_box() is None → return` guard was part
        of the invisible-failure chain."""
        render = FakeRenderHandler()
        handler = ChatHandler(
            FakeMainContent(None, current_session_key="project:alpha"),
            None, None)
        handler._chat_render_handler = render
        handler.set_agent_runtime_handler(
            RaisingReceiver(RuntimeError("No provider configured.")))
        with caplog.at_level(logging.WARNING, logger="ui.handlers.chat_handler"):
            handler._send_local("agent:main", "hello")
        assert len(render.calls) == 1
        assert render.calls[0]["session_key"] == "project:alpha"
