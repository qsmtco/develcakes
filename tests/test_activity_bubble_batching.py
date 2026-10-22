# tests/test_activity_bubble_batching.py
# SPEC-UI-RESPONSIVENESS-2 §2.4 Phase 4 Part C — activity bubbles behind the
# 250 ms cadence.
#
# Before: every tool start/result emitted its ActivityBubble straight to the
# drawer (one dispatch per event). After: AgentRuntimeHandler queues bubbles
# per session and delivers them in FIFO order from a single 250 ms flush —
# the same cadence ActivityHandler._status_tick uses. Bubbles are never
# dropped and never reordered; the turn-end lifecycle separator is emitted
# only after the session's queued bubbles have been drained.
#
# Dispatch bound: N rapid events inside one 250 ms window produce
# <= ceil(N / ACTIVITY_BUBBLE_FLUSH_MS) flush dispatches (one, for a burst).
#
# GLib is supplied via the shared `fake_glib` fixture, which records armed
# timers and never fires them automatically — the tests fire them explicitly.
# NOTE: that fixture records (source_id, delay_ms, callback) and drops the
# callback args, so firing a flush passes the session key explicitly.

from __future__ import annotations

import math
from datetime import datetime, timezone
from unittest.mock import MagicMock

from agent.special_agents import SpecialAgentDef


def _make_handler(fake_glib):
    from ui.handlers.agent_runtime_handler import AgentRuntimeHandler

    handler = AgentRuntimeHandler(MagicMock(), MagicMock(), GLib_module=fake_glib)
    handler._fh = MagicMock()
    # The crh double must be typed for the non-streaming path: a MagicMock
    # return from is_streaming() is truthy, which would send
    # _do_response_complete down the streaming branch with a MagicMock text
    # (same setup the existing drawer-emission tests use).
    handler._crh.is_streaming.return_value = False
    handler._active_project = ("test", "/tmp/test")
    handler._agents = {
        "agent:coder": SpecialAgentDef(
            conv_id_prefix="agent:coder",
            display_name="Coder",
            role="coder",
            emoji="🛠️",
            tools=["read_file", "write_file", "exec_command"],
            can_write=True,
        )
    }
    handler._pending_tool_args = {}
    handler._ended_sessions = set()
    handler._session_completed = set()
    delivered = []
    handler.set_on_activity_bubble(delivered.append)
    return handler, delivered


def _fire_armed_flush(fake_glib, session_key):
    """Run the recorded 250 ms flush timer for `session_key`."""
    flushes = [t for t in fake_glib.armed if t[1] == 250]
    assert flushes, "no 250 ms flush timer was armed"
    _sid, _delay, callback = flushes[0]
    callback(session_key)


class TestBatchedBubbleDelivery:
    def test_rapid_tool_events_flush_once_in_order(self, fake_glib):
        """Six events in one burst → ONE flush, all six delivered, in order.

        RED pre-Phase-4: each event was dispatched on its own (6 dispatches
        before any timer was armed).
        """
        handler, delivered = _make_handler(fake_glib)
        n_events = 6

        for i in range(3):
            handler._do_tool_call_start("agent:coder", "read_file", {"path": f"f{i}.txt"})
            handler._pending_tool_args["agent:coder"] = {"path": f"f{i}.txt"}
            handler._do_tool_call_result("agent:coder", "read_file", "content", success=True)

        # Nothing is delivered before the cadence tick.
        assert delivered == [], (
            "bubbles must wait for the 250 ms flush, got "
            f"{[b.type for b in delivered]}"
        )

        flushes = [t for t in fake_glib.armed if t[1] == 250]
        assert len(flushes) <= math.ceil(n_events / handler.ACTIVITY_BUBBLE_FLUSH_MS), (
            f"a burst of {n_events} events armed {len(flushes)} flushes"
        )
        assert len(flushes) == 1, "one burst must arm exactly one flush timer"

        _fire_armed_flush(fake_glib, "agent:coder")

        assert [b.type for b in delivered] == [
            "tool_start", "tool_end",
            "tool_start", "tool_end",
            "tool_start", "tool_end",
        ], "all bubbles must be delivered, in emission order"
        assert delivered[-1].status.name == "SUCCESS", "final state must be the tool_end"
        assert all(b.session_key == "agent:coder" for b in delivered)

    def test_flush_rearms_for_a_later_burst(self, fake_glib):
        """After a flush, the next event arms a fresh timer (no lost bubbles)."""
        handler, delivered = _make_handler(fake_glib)

        handler._do_tool_call_start("agent:coder", "read_file", {"path": "a.txt"})
        _fire_armed_flush(fake_glib, "agent:coder")
        assert [b.type for b in delivered] == ["tool_start"]

        handler._pending_tool_args["agent:coder"] = {"path": "a.txt"}
        handler._do_tool_call_result("agent:coder", "read_file", "content", success=True)
        assert [b.type for b in delivered] == ["tool_start"], "second burst waits too"

        _fire_armed_flush(fake_glib, "agent:coder")
        assert [b.type for b in delivered] == ["tool_start", "tool_end"]

    def test_error_and_patch_bubbles_keep_their_order(self, fake_glib):
        """write_file success → patch (not tool_end) still rides the batch."""
        handler, delivered = _make_handler(fake_glib)

        handler._do_tool_call_start("agent:coder", "write_file", {"path": "src/main.py"})
        handler._do_tool_call_result("agent:coder", "write_file", "OK — wrote 12 bytes to src/main.py", success=True)
        handler._do_tool_call_start("agent:coder", "exec_command", {"command": "false"})
        handler._pending_tool_args["agent:coder"] = {"command": "false"}
        handler._do_tool_call_result("agent:coder", "exec_command", "boom", success=False)

        _fire_armed_flush(fake_glib, "agent:coder")

        assert [b.type for b in delivered] == [
            "tool_start", "patch", "tool_start", "tool_error",
        ], f"order/type drift: {[b.type for b in delivered]}"

    def test_per_session_queues_are_independent(self, fake_glib):
        """One session's flush must not deliver another session's bubbles."""
        handler, delivered = _make_handler(fake_glib)
        handler._agents["agent:debugger"] = SpecialAgentDef(
            conv_id_prefix="agent:debugger",
            display_name="Debugger",
            role="debugger",
            emoji="🔍",
            tools=["read_file"],
            can_write=False,
        )

        handler._do_tool_call_start("agent:coder", "read_file", {"path": "a.txt"})
        handler._do_tool_call_start("agent:debugger", "read_file", {"path": "b.txt"})
        assert len([t for t in fake_glib.armed if t[1] == 250]) == 2

        _fire_armed_flush(fake_glib, "agent:debugger")

        assert [b.session_key for b in delivered] == ["agent:debugger"]
        assert handler._bubble_queue.get("agent:coder"), "coder's bubble is still queued"

    def test_no_glib_delivers_inline(self):
        """No main loop (tests / direct callers) → immediate delivery."""
        from ui.handlers.agent_runtime_handler import AgentRuntimeHandler
        handler = AgentRuntimeHandler(MagicMock(), MagicMock(), GLib_module=None)
        handler._agents = {}
        delivered = []
        handler.set_on_activity_bubble(delivered.append)

        from models.activity import ActivityBubble
        handler._emit_activity_bubble(ActivityBubble(type="tool_start", session_key="s1"))

        assert len(delivered) == 1 and delivered[0].type == "tool_start"


class TestTurnEndOrdering:
    def test_queued_bubbles_flush_before_the_drawer_end_separator(self, fake_glib):
        """The end separator must never precede a queued tool row."""
        handler, _delivered = _make_handler(fake_glib)
        order = []
        handler.set_on_activity_bubble(lambda b: order.append(("bubble", b.type)))
        handler.set_on_drawer_lifecycle(
            lambda sk, name, phase: order.append(("lifecycle", phase))
        )

        handler._do_tool_call_start("agent:coder", "read_file", {"path": "a.txt"})
        assert order == [], "fixture precondition: the bubble is queued, not delivered"

        handler._do_response_complete("agent:coder", "Done")

        assert order == [("bubble", "tool_start"), ("lifecycle", "end")], (
            f"drawer ordering broken: {order}"
        )

    def test_error_path_also_flushes_first(self, fake_glib):
        handler, _delivered = _make_handler(fake_glib)
        order = []
        handler.set_on_activity_bubble(lambda b: order.append(("bubble", b.type)))
        handler.set_on_drawer_lifecycle(
            lambda sk, name, phase: order.append(("lifecycle", phase))
        )

        handler._do_tool_call_start("agent:coder", "exec_command", {"command": "ls"})
        handler._do_error("agent:coder", "Cancelled by user")

        assert order == [("bubble", "tool_start"), ("lifecycle", "end")], (
            f"drawer ordering broken on the error path: {order}"
        )

    def test_manual_flush_cancels_the_armed_timer(self, fake_glib):
        """flush_pending_activity_bubbles drains now and drops the timer."""
        handler, delivered = _make_handler(fake_glib)

        handler._do_tool_call_start("agent:coder", "read_file", {"path": "a.txt"})
        timer_id = [t for t in fake_glib.armed if t[1] == 250][0][0]

        handler.flush_pending_activity_bubbles("agent:coder")

        assert [b.type for b in delivered] == ["tool_start"]
        assert timer_id not in fake_glib.armed_ids, "the armed timer must be removed"
        assert "agent:coder" not in handler._bubble_timers
        # Firing the stale callback again must not duplicate the delivery.
        handler._flush_activity_bubbles("agent:coder")
        assert [b.type for b in delivered] == ["tool_start"]
