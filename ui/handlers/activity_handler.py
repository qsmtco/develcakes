"""
ActivityHandler — 6-state activity machine driving the shared activity-status pill
(project bar — UI-PILLBAR relocation; status_target duck-type, SPEC-07 R4).

States: idle | sending | reasoning | streaming | tool_use | done

Transitions triggered by the local agent runtime:
  agent start        → reasoning
  agent end          → done (auto → idle after 5s)
  tool start         → tool_use
  first text delta   → streaming

Owns all state machine state (timers, counters, timestamps).
Does NOT own the status target or MainContent — received as constructor dependencies.
Thread safety: all GTK calls via GLib.idle_add().
"""

from __future__ import annotations

import logging
import time

_logger = logging.getLogger(__name__)


class ActivityHandler:
    _STATES = ("idle", "sending", "reasoning", "streaming", "tool_use", "done")
    PREFlight_TIMEOUT_SEC = 30

    def __init__(self, status_target, main_content, GLib_module=None):
        self._status_target = status_target
        self._mc = main_content
        self._GLib = GLib_module or __import__("gi.repository.GLib", fromlist=["GLib"]).GLib

        # State machine state
        self._state = "idle"
        self._streaming_token_count = 0
        self._first_delta_seen = False
        self._current_tool_name = ""

        # Timers (GLib source IDs) — _done_flash_timers is per-session
        # AC3 Phase 1 Part C: ONE 250ms ticker (_status_ticker_id) drives both
        # the live-update branch (reasoning/streaming/tool_use) and the
        # idle-pulse branch (idle). _live_update_timer / _idle_pulse_timer are
        # kept as aliases maintained by _stop_live_update/_stop_idle_pulse so
        # existing call sites keep working.
        self._status_ticker_id: int | None = None
        self._live_update_timer = None   # alias for _status_ticker_id
        self._idle_pulse_timer = None    # alias for _status_ticker_id
        self._last_tick_signature: tuple | None = None  # skip-when-unchanged cache
        # UIRESP3 Phase 2: idle-pulse budget — the idle branch of the 250ms
        # ticker may pulse at most _IDLE_TICK_BUDGET times per idle entry,
        # then the source dies (bounded animation instead of an unbounded one).
        self._idle_ticks = 0
        self._done_flash_timers: dict[str, int] = {}  # session_key → GLib source ID
        self._send_initiated_timers: dict[str, int] = {}  # session_key → pre-flight timeout

        # Per-session timing
        self._agent_start_time: dict[str, float] = {}

        # Per-session progress tracking (two-phase: time-driven → event-driven)
        self._phase: dict[str, int] = {}  # session_key → 1 (time-driven) or 2 (event-driven)
        self._event_hop_count: dict[str, int] = {}  # session_key → number of gateway events received

    # ── Public entry points (called from the local agent runtime) ──

    def on_agent_start(self, session_key, data=None):
        """agent phase=start — enter reasoning state."""
        sk = self._active_session() or session_key
        self._agent_start_time[sk] = time.monotonic()
        self._streaming_token_count = 0
        self._first_delta_seen = False
        self._current_tool_name = ""
        self._set_state("reasoning", sk)

    def on_agent_end(self, session_key, data=None):
        """agent phase=end — enter done state, auto-idle after 5s."""
        sk = self._active_session() or session_key
        self._agent_start_time.pop(sk, None)
        self._reset_session_state(sk)
        self._set_state("done", sk)
        self._start_done_flash(sk)

    def on_tool_use(self, tool_name, session_key, data=None):
        """tool_call event — enter tool_use state."""
        self._current_tool_name = tool_name or ""
        sk = self._active_session() or session_key
        self._set_state("tool_use", sk)

    def on_chat_delta(self, delta_text, session_key):
        """chat delta (streaming) — first delta transitions to streaming state."""
        sk = self._active_session() or session_key
        count = len(delta_text) if delta_text else 0
        self._streaming_token_count += count

        if delta_text and (
            (not self._first_delta_seen) or self._state == "tool_use"
        ):
            self._first_delta_seen = True
            self._set_state("streaming", sk)

    def set_agent_routing(self, routing_table) -> None:
        """Inject AgentRoutingTable. Called by window.py._build().

        Used by _is_ui_active to resolve project tabs for agent session keys.
        """
        self._agent_to_project = routing_table

    def _get_progress_session(self, session_key: str | None) -> str:
        """Return the session key to use for progress tracking (active session or provided)."""
        return self._active_session() or session_key or "_global_"

    def _reset_progress(self, sk: str):
        """Reset progress state for a session — called on send_initiated."""
        self._phase[sk] = 1
        self._event_hop_count[sk] = 0

    def _reset_session_state(self, sk: str):
        """Clean up all progress state for a session (on idle/error)."""
        self._phase.pop(sk, None)
        self._event_hop_count.pop(sk, None)
        self._agent_start_time.pop(sk, None)

    # ── State machine internals ─────────────────────────────────────────────

    def _active_session(self) -> str | None:
        """Return the currently active UI session_key from MainContent, or None."""
        if self._mc is not None:
            try:
                return self._mc.get_current_session_key()
            except Exception:
                pass
        return None

    def _is_ui_active(self, session_key: str | None) -> bool:
        """True if the given session_key matches the currently active UI session.

        Used to guard state transitions — if the UI is showing a different tab
        than the one this event belongs to, we skip the update (deadcode pattern).

        When the active tab is a project tab and the event belongs to an agent
        that is a member of that project, we resolve the agent key to the project
        tab key so the state transition is not incorrectly skipped.
        """
        if session_key is None:
            return True
        active = self._active_session()
        if active is None or session_key == active:
            return True
        # Resolve project tab for agent key — if the active tab is the project
        # tab for this agent, consider it active.
        if self._agent_to_project is not None:
            project_name = self._agent_to_project.get_project(session_key)
            if project_name is not None and f"project:{project_name}" == active:
                return True
        return False

    def _set_state(self, state: str, session_key: str | None):
        """Transition to a new state, cleaning up old timers and starting new ones."""
        # Guard: ignore events for sessions not currently displayed in UI
        if not self._is_ui_active(session_key):
            return

        if state == self._state:
            # Even if already in this state, still update the pill for live counters
            if state in ("reasoning", "streaming", "tool_use"):
                self._last_tick_signature = None  # manual render invalidates the tick cache
                self._update_status()
            elif state == "idle":
                # Audit BUG #1: a same-state idle re-entry (e.g. a delayed
                # on_agent_error for a session that already finished) must
                # restart the pulse budget. Without this the ticker is already
                # dead from the previous budget, _idle_ticks is still at its
                # exhausted value, and the pulse never comes back until some
                # unrelated real transition happens.
                #
                # GUARD ON LIVENESS: only re-arm when no ticker is live. A
                # live ticker means the budget is still running — arming again
                # would install a SECOND concurrent 250ms source (double-rate
                # cost, the very thing this phase bounds). The dying tick sets
                # _status_ticker_id = None, so that is the liveness indicator.
                # We deliberately do NOT call _stop_idle_pulse() here: on the
                # dead path the id is already None, and on the live path GLib
                # would only be asked to remove a source we still want.
                if self._status_ticker_id is None:
                    self._idle_ticks = 0
                    self._status_ticker_id = self._GLib.timeout_add(
                        250, self._status_tick)
                    self._live_update_timer = self._status_ticker_id
                    self._idle_pulse_timer = self._status_ticker_id
            return

        self._state = state

        # UIRESP3 Phase 2: every transition grants a fresh idle-pulse budget.
        self._idle_ticks = 0

        # Clean up all timers from previous state
        self._stop_live_update()
        self._stop_idle_pulse()
        if session_key is not None:
            self._stop_done_flash(session_key)
            self._stop_send_initiated_timer(session_key)
        else:
            self._stop_done_flash()
            self._stop_send_initiated_timer()

        # Apply state to the status target (activity pill)
        self._update_status()

        # Start the single 250ms status ticker for the new state (AC3 Phase 1
        # Part C). The tick branches on self._state: active states run the
        # live-update branch, idle runs the pulse branch.
        self._status_ticker_id = self._GLib.timeout_add(250, self._status_tick)
        # Keep aliases in sync — _stop_live_update/_stop_idle_pulse read them
        self._live_update_timer = self._status_ticker_id
        self._idle_pulse_timer = self._status_ticker_id
        # done: flash timer started by caller

    def _update_status(self):
        """Update the activity pill (plain text + state) to reflect current state."""
        state = self._state
        if state == "idle":
            text = "● Idle"
        elif state == "sending":
            text = "⬡ Pre Flight Check"
        elif state == "reasoning":
            text = "◉ Reasoning…"
        elif state == "streaming":
            text = self._streaming_label()
        elif state == "tool_use":
            text = f"⚙ {self._current_tool_name}"
        else:  # done
            text = "✓ Done"
        self._status_target.set_status_text(text, state)

    def _streaming_label(self) -> str:
        """Build live counter label for streaming state (plain text)."""
        sk = self._active_session()
        token_est = self._streaming_token_count // 4
        start = self._agent_start_time.get(sk) if sk else None
        elapsed = time.monotonic() - start if start else 0
        elapsed_str = f"{elapsed:.1f}s"
        velocity = token_est / elapsed if elapsed > 0.1 else 0
        vel_str = f"{velocity:.0f} tok/s"
        return f"⬇ Generating… · {token_est} tokens · {vel_str} · {elapsed_str}"

    # ── Timer callbacks ────────────────────────────────────────────────────

    def _status_tick(self):
        """Single 250ms tick for the whole status machine (AC3 Phase 1 Part C).

        Branches on self._state:
          reasoning/streaming/tool_use → live-update branch (counters).
          idle                         → idle-pulse branch (keep-alive; no render).
        Returns GLib's keep-alive contract: True while the current state needs
        ticking, False when the state no longer matches (timer dies).
        """
        if self._state in ("reasoning", "streaming", "tool_use"):
            self._live_update()
            return True
        if self._state == "idle":
            self._idle_pulse()
            # UIRESP3 Phase 2: bounded keep-alive — ~5 s at 250 ms, then the
            # idle budget is exhausted and the source dies cleanly (no render
            # calls fire — the pill keeps its last state).
            self._idle_ticks += 1
            if self._idle_ticks < 20:
                return True
            # Clear our own bookkeeping before the source dies — GLib drops the
            # callback on a False return, so these ids are stale from here on.
            # Without this a later same-state-idle re-entry would call
            # source_remove() on an already-dead id (GLib critical warning).
            # (Audit BUG #1 companion.)
            self._status_ticker_id = None
            self._live_update_timer = None
            self._idle_pulse_timer = None
            return False
        # Fallthrough: state is sending/done — no branch matches, so the source
        # dies here too. Clear the same bookkeeping as the idle branch: GLib
        # drops the callback on a False return, so a stale non-None id would
        # make the next _stop_live_update/_stop_idle_pulse call source_remove()
        # a dead id (real-GLib warning; confirmed).
        # (Audit BUG #2: incomplete cleanup at the fallthrough.)
        self._status_ticker_id = None
        self._live_update_timer = None
        self._idle_pulse_timer = None
        return False

    def _live_update(self):
        """Live-update branch — update counters during reasoning/streaming/tool_use.

        Skip-when-unchanged: if (state, phase, hop bucket, elapsed bucket) is
        identical to the last rendered tick, skip both the status rebuild
        (plain text) and the _streaming_label() construction.
        """
        if self._state not in ("reasoning", "streaming", "tool_use"):
            return False
        sk = self._active_session()
        signature = (
            self._state,
            self._phase.get(self._get_progress_session(None), 1),
            self._event_hop_count.get(self._get_progress_session(None), 0),
            # Elapsed bucket: 1.0s granularity — labels update at most once
            # per second for pure time drift; hop/state changes rebuild sooner.
            int((time.monotonic() - self._agent_start_time.get(sk, time.monotonic())) / 1.0),
        )
        if signature == self._last_tick_signature:
            return True  # nothing changed since the last rendered tick

        self._last_tick_signature = signature
        self._update_status()
        return True

    def _idle_pulse(self):
        """Idle keep-alive decision for the 250ms ticker (AC3 Phase 1 Part C).

        The pulse RENDER died with the progress bar (SPEC-07 R4, SP2) — what
        remains is the ticker's branch decision: True while idle keeps the
        source alive (the UIRESP3 Phase 2 budget in _status_tick still bounds
        it); False kills it. Kept as a method because _status_tick's branch
        structure (and its skip-gating asymmetry) is pinned by test rows that
        predate the bar's removal.
        """
        return self._state == "idle"

    def _start_done_flash(self, session_key: str | None):
        """Start 5-second done→idle flash timer for the given session."""
        sk = session_key
        if sk is None:
            return

        def expire(s: str):
            # Remove from dict first
            self._done_flash_timers.pop(s, None)
            # Only transition if still in done and UI is on this session
            if self._state == "done" and self._is_ui_active(s):
                self._set_state("idle", s)
            return False

        timer_id = self._GLib.timeout_add_seconds(5, lambda: expire(sk))
        self._done_flash_timers[sk] = timer_id

    # ── Timer cleanup ─────────────────────────────────────────────────────

    def _stop_live_update(self):
        if self._live_update_timer is not None:
            self._GLib.source_remove(self._live_update_timer)
            self._live_update_timer = None

    def _stop_idle_pulse(self):
        if self._idle_pulse_timer is not None:
            self._GLib.source_remove(self._idle_pulse_timer)
            self._idle_pulse_timer = None

    def _stop_done_flash(self, session_key: str | None = None):
        """Stop the done flash timer for a specific session (or all if None)."""
        if session_key is not None:
            timer_id = self._done_flash_timers.pop(session_key, None)
            if timer_id is not None:
                self._GLib.source_remove(timer_id)
        else:
            for timer_id in self._done_flash_timers.values():
                self._GLib.source_remove(timer_id)
            self._done_flash_timers.clear()

    def _stop_send_initiated_timer(self, session_key: str | None = None):
        """Stop the pre-flight timeout timer for a specific session (or all if None)."""
        if session_key is not None:
            timer_id = self._send_initiated_timers.pop(session_key, None)
            if timer_id is not None:
                self._GLib.source_remove(timer_id)
        else:
            for timer_id in self._send_initiated_timers.values():
                self._GLib.source_remove(timer_id)
            self._send_initiated_timers.clear()
