"""
ActivityHandler — 6-state activity machine driving the shared activity-status pill
(project bar — UI-PILLBAR relocation; status_target duck-type, SPEC-07 R4).

States: idle | sending | reasoning | streaming | tool_use | done

Transitions triggered by gateway events wired from window._on_ws_event():
  agent phase=start  → reasoning
  agent phase=end     → done (auto → idle after 5s)
  agent phase=error   → idle
  tool_call event    → tool_use
  first chat delta   → streaming
  agent message      → sending (pre-flight)

Owns all state machine state (timers, counters, timestamps).
Does NOT own the status target or MainContent — received as constructor dependencies.
Thread safety: all GTK calls via GLib.idle_add().
"""

from __future__ import annotations

import logging
import time
from typing import Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from models.activity import ActivityBubble

_logger = logging.getLogger(__name__)

# AC3 Phase 1 Part C: sentinel for the per-event agent-name cache — distinct
# from any real resolved name (including "" and None).
_AGENT_NAME_UNRESOLVED = object()


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

        # Bug fix: state tracking for missing message recovery (Phase 1 of SPEC-smarter-chat-ux)
        self._assistant_text_buffer: dict[str, str] = {}    # session_key → last assistant text
        self._lifecycle_ended: dict[str, bool] = {}       # run_id → True when lifecycle end fired
        self._on_assistant_buffer: Callable[[str, str], None] | None = None  # buffer fwd callback
        self._lifecycle_completed_callback: Callable[[str, str], None] | None = None  # cb(sk, text)
        self._activity_bubble_callback: Callable[['ActivityBubble'], None] | None = None  # cb(bubble)
        self._on_agent_start_callback: Callable[[str], None] | None = None  # cb(sk) — clears render guard
        # SPEC-activity-drawer Phase 1: lifecycle separator callback.
        # cb(session_key, agent_name, "start"|"end") — drawer uses this to insert
        # per-agent separator rows. agent_name may be "" (drawer defaults to "Agent").
        self._on_agent_lifecycle: Callable[[str, str, str], None] | None = None
        # PHASE 6: AgentManager for session_key → agent_name fallback when the
        # gateway payload's data.agentName is empty (SPEC §2.4 fallback chain).
        self._agent_mgr = None
        # AC3 Phase 1 Part C: one-shot agent-name cache, reset at the top of
        # every on_gateway_event. Guarantees ≤1 _resolve_agent_name call per
        # event and zero calls on events that never need the name.
        self._resolved_agent_name: object = _AGENT_NAME_UNRESOLVED

    # ── Public entry points (called from gateway event handlers in window) ──

    def on_agent_start(self, session_key, data=None):
        """agent phase=start — enter reasoning state."""
        sk = self._active_session() or session_key
        self._agent_start_time[sk] = time.monotonic()
        self._streaming_token_count = 0
        self._first_delta_seen = False
        self._current_tool_name = ""
        self._set_state("reasoning", sk)
        # Clear render guard from previous round so new responses can render
        if self._on_agent_start_callback:
            self._on_agent_start_callback(session_key)

    def on_agent_end(self, session_key, data=None):
        """agent phase=end — enter done state, auto-idle after 5s."""
        sk = self._active_session() or session_key
        self._agent_start_time.pop(sk, None)
        self._reset_session_state(sk)
        self._set_state("done", sk)
        self._start_done_flash(sk)

    def on_agent_error(self, session_key, data=None):
        """agent phase=error — return to idle immediately."""
        sk = self._active_session() or session_key
        self._set_state("idle", sk)

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

        if not self._first_delta_seen:
            self._first_delta_seen = True
            self._set_state("streaming", sk)

    def on_agent_message_received(self, session_key):
        """agent message in history — pre-flight signal (brief sending state)."""
        sk = self._active_session() or session_key
        self._set_state("sending", sk)

    def on_chat_final(self, session_key):
        """chat final — no state change here; on_agent_end handles completion."""
        pass

    def _extract_chat_text(self, payload: dict) -> str:
        """Extract plain text from a gateway chat event payload.

        The gateway sends chat event payloads with the text at payload.message.content,
        in one of two forms:
        - A string (simple text responses)
        - A list of typed blocks (block-level formatting: code, quote, media, etc.)

        This helper normalizes both forms into a single string for token counting.
        It is a local copy of chat_handler._extract_text (ui/handlers/chat_handler.py:645)
        to keep handlers decoupled — see tests/conftest.py::test_handlers_do_not_import_each_other
        for the rule. If a third handler ever needs the same logic, promote to
        a shared module (out of scope for this phase).
        """
        msg_obj = payload.get("message", {})
        if isinstance(msg_obj, dict):
            content = msg_obj.get("content", "")
        else:
            content = msg_obj
        if isinstance(content, list):
            parts = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type", "")
                if block_type == "text":
                    t = block.get("text", "")
                    if t:
                        parts.append(t)
                elif block_type == "input_image":
                    # Gateway media attachment — not text, but include as a marker
                    # so the token count reflects the response's size in spirit.
                    # The image content itself is not counted (we cannot estimate
                    # its token cost from a URL); we count a 0-length placeholder
                    # by simply not appending. Token velocity is dominated by text.
                    continue
            return "".join(parts)
        elif isinstance(content, str):
            return content
        return str(content) if content else ""

    def set_on_assistant_buffer(self, cb):
        """Set callback for buffering assistant text: cb(session_key, text).
        ActivityHandler calls this after every stream=assistant event so ChatHandler
        can maintain its own buffer for the missing-message recovery path.
        """
        self._on_assistant_buffer = cb

    def set_on_lifecycle_completed(self, cb):
        """Set callback for lifecycle end: cb(session_key, buffered_text).

        ActivityHandler calls this when the agent round-trip ends (phase=end or
        phase=error). ChatHandler uses this to render the fallback bubble when
        no chat final arrived with a message body.

        Architecture: ActivityHandler only tracks state — it never renders.
        ChatHandler makes the render decision via this callback.
        """
        self._lifecycle_completed_callback = cb

    def set_on_agent_start(self, cb):
        """Set callback for agent round start: cb(session_key).

        ActivityHandler calls this when lifecycle phase=start fires. ChatHandler
        uses this to clear the render guard from the previous round so that
        subsequent responses for the same session are not blocked.
        """
        self._on_agent_start_callback = cb

    def set_on_activity_bubble(self, cb: Callable[['ActivityBubble'], None]):
        """Set callback for activity bubbles: cb(activity_bubble).

        ActivityHandler calls this for each tool/plan/approval/command_output/patch
        event to generate a system bubble in the chat. ChatHandler renders via
        build_role_bubble(role='System', text=bubble.format_text()).
        """
        self._activity_bubble_callback = cb

    def set_on_agent_lifecycle(self, cb: Callable[[str, str, str], None]) -> None:
        """Set callback for agent lifecycle: cb(session_key, agent_name, phase).

        phase is "start" or "end". ActivityHandler fires this on every
        stream=lifecycle phase=start and phase=end (or phase=error) event.
        The drawer uses this to insert per-agent separator rows.

        agent_name comes from payload.data.agentName when present; otherwise "".
        """
        self._on_agent_lifecycle = cb

    def set_agent_routing(self, routing_table) -> None:
        """Inject AgentRoutingTable. Called by window.py._build().

        Used by _is_ui_active to resolve project tabs for agent session keys.
        """
        self._agent_to_project = routing_table

    def set_agent_manager(self, agent_mgr) -> None:
        """Inject AgentManager for session_key → agent_name fallback (SPEC-activity-drawer §2.4).

        Called by ConnectionSyncHandler.sync() after the gateway connects.
        Used to resolve agent_name when the gateway payload's data.agentName is empty.
        """
        self._agent_mgr = agent_mgr
    def _safe_data(self, payload: dict) -> dict:
        """Safely extract payload.data — handles missing, None, and non-dict values.

        PHASE 7 Bug #4: dict.get('data', {}) returns the default only when the
        key is MISSING. When the key is present-but-null (data: None) or a
        non-dict value, the default is bypassed and downstream .get() crashes
        with AttributeError. This helper coerces any non-dict result to {} so
        every call site is safe.
        """
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    def _resolve_agent_name(self, payload: dict) -> str:
        """Resolve the agent display name from a gateway payload.

        Resolution order (SPEC-activity-drawer §2.4 fallback chain):
        1. payload.data.agentName — gateway-supplied agent name (may be empty)
        2. AgentManager.get_name(payload.sessionKey) — local session_key → name lookup
        3. "" — drawer will display "[Agent]" as last-resort fallback

        Args:
            payload: The gateway event payload dict.

        Returns:
            The agent display name, or "" if unknown.
        """
        direct = self._safe_data(payload).get("agentName", "") or ""
        if direct:
            return direct
        session_key = payload.get("sessionKey", "") or ""
        if session_key and self._agent_mgr is not None:
            try:
                name = self._agent_mgr.get_name(session_key)
                if name:
                    return name
            except Exception:
                pass  # AgentManager may not be ready; fall through
        return ""

    def _agent_name_for_event(self, payload: dict) -> str:
        """Resolve the agent name at most ONCE per gateway event (AC3 Phase 1 Part C).

        Lazily delegates to _resolve_agent_name on first access within an
        event; subsequent accesses reuse the cached value. The cache is reset
        at the top of on_gateway_event, so the cost profile is:
          - events that never need the name (assistant deltas, res, tick…): 0 calls
          - events that need it once or more (item/plan/approval/patch/lifecycle): exactly 1 call

        DO NOT call from outside on_gateway_event's dynamic extent — the cache
        has no TTL and would go stale across events.
        """
        if self._resolved_agent_name is _AGENT_NAME_UNRESOLVED:
            self._resolved_agent_name = self._resolve_agent_name(payload)
        return self._resolved_agent_name

    def on_send_initiated(self, session_key: str):
        """Send button pressed — enter pre-flight (sending) state with 30s timeout.

        Resets progress to phase 1 (time-driven). If no res arrives within 30s,
        revert to idle and clear progress.
        """
        sk = self._active_session() or session_key
        self._stop_send_initiated_timer(sk)
        self._reset_progress(sk)
        self._set_state("sending", sk)
        timer_id = self._GLib.timeout_add_seconds(
            self.PREFlight_TIMEOUT_SEC,
            lambda: self._on_preflight_timeout(sk),
        )
        self._send_initiated_timers[sk] = timer_id

    def on_res_confirmed(self, session_key: str):
        """Gateway res confirmed our send — end phase 1, transition to phase 2 (event-driven).

        Called when ChatHandler receives a res matching our pending req_id.
        """
        sk = self._active_session() or session_key
        self._stop_send_initiated_timer(sk)
        # Phase 2: every gateway event now hops the bar
        self._phase[sk] = 2
        self._agent_start_time[sk] = time.monotonic()
        self._set_state("reasoning", sk)

    def on_gateway_event(self, event: str, payload: dict):
        """Universal entry point for all gateway events.

        Every event increments hop count in phase 2 (event-driven). State transitions
        are delegated to specific methods. tick and health have no state handler, so they
        only contribute to progress without causing a state change.
        """
        session_key = payload.get("sessionKey", "") or ""
        sk = self._get_progress_session(session_key)

        # AC3 Phase 1 Part C: reset the one-shot agent-name cache — every
        # event gets exactly one resolution budget, spent lazily.
        self._resolved_agent_name = _AGENT_NAME_UNRESOLVED

        # Phase 2: every event hops the bar (skip idle/done — round is over)
        if self._phase.get(sk, 1) == 2 and self._state not in ("idle", "done"):
            self._event_hop_count[sk] = self._event_hop_count.get(sk, 0) + 1
            # TEMPORARILY DISABLED 2026-04-22: Investigating UI freeze on large pastes.
            # Hypothesis: 100+ gateway events during agent response each call
            # _update_status(), queuing too many GLib.idle_add callbacks and
            # starving GTK's render/input loop. If disabling this fixes the freeze,
            # the fix is to throttle _update_status() to e.g. max once per 200ms.
            # TODO: Uncomment the line below once throttling is implemented.
            # self._update_status()

        # ── Bug fix: buffer assistant text for fallback rendering ──────────
        if event == "agent":
            stream = payload.get("stream", "")
            if stream == "assistant":
                text = self._safe_data(payload).get("text", "")
                if text:
                    sk = payload.get("sessionKey", "") or session_key
                    if sk:
                        self._assistant_text_buffer[sk] = text
                        if self._on_assistant_buffer:
                            self._on_assistant_buffer(sk, text)
            elif stream == "lifecycle":
                phase = self._safe_data(payload).get("phase", "")
                # Resolve agent name with AgentManager fallback (SPEC-activity-drawer §2.4 / PHASE 6).
                # When the gateway's data.agentName is empty, fall back to AgentManager.
                _agent_name = self._agent_name_for_event(payload)
                # Track lifecycle end for missing-message recovery.
                # Cleanup runs on both end and error — fixes memory leak.
                if phase in ("end", "error"):
                    run_id = payload.get("runId", "") or ""
                    sk = payload.get("sessionKey", "") or session_key
                    # Fire lifecycle-completed callback so ChatHandler can render fallback.
                    # text is the last buffered assistant text for this session.
                    if sk and self._lifecycle_completed_callback:
                        text = self._assistant_text_buffer.get(sk, "")
                        self._lifecycle_completed_callback(sk, text)
                    if sk:
                        self._assistant_text_buffer.pop(sk, None)
                    if run_id:
                        self._lifecycle_ended.pop(run_id, None)
                    # SPEC-activity-drawer: fire agent_lifecycle "end" so the drawer
                    # can insert a per-agent summary separator row.
                    if sk and self._on_agent_lifecycle:
                        self._on_agent_lifecycle(sk, _agent_name, "end")
                elif phase == "start":
                    # ── Activity bubble: lifecycle start ──────────────────
                    sk = payload.get("sessionKey", "") or session_key
                    if sk and self._activity_bubble_callback:
                        from models.activity import ActivityBubble, ToolStatus
                        bubble = ActivityBubble(type="lifecycle_start", session_key=sk,
                                                agent_name=_agent_name, icon="⏳")
                        self._activity_bubble_callback(bubble)
                    # SPEC-activity-drawer: fire agent_lifecycle "start" so the
                    # drawer can insert a per-agent separator row.
                    if sk and self._on_agent_lifecycle:
                        self._on_agent_lifecycle(sk, _agent_name, "start")
            elif stream == "item":
                # ── Activity bubble: item events (tool/command/patch) ────────
                # NOTE: stream="tool" events are NOT broadcast to clients — only sent to
                # toolEventRecipients. But stream="item" events ARE broadcast and carry
                # kind="tool" / kind="command" / kind="patch" with phase, name, title, status.
                # This is why exec bubbles worked (command_output is also broadcast) but
                # tool_start/tool_end never appeared (stream="tool" never reaches us).
                data = self._safe_data(payload)
                kind = data.get("kind", "")
                item_phase = data.get("phase", "")
                item_name = data.get("name", "") or ""
                item_status = data.get("status", "")
                started_at = data.get("startedAt")
                ended_at = data.get("endedAt")
                # SPEC-activity-drawer §2.4: tool bubbles carry agent_name with
                # AgentManager fallback (PHASE 6). When the gateway's data.agentName
                # is empty on stream=item kind=tool events, fall back to AgentManager.
                _agent_name = self._agent_name_for_event(payload)
                sk = payload.get("sessionKey", "") or session_key

                if kind == "tool" and self._activity_bubble_callback:
                    from models.activity import ActivityBubble, ToolStatus
                    if item_phase == "start":
                        self._activity_bubble_callback(
                            ActivityBubble(type="tool_start", session_key=sk, tool_name=item_name,
                                           icon="🔧", status=ToolStatus.RUNNING,
                                           agent_name=_agent_name)
                        )
                    elif item_phase == "end":
                        is_error = item_status == "failed"
                        icon = "❌" if is_error else "✅"
                        btype = "tool_error" if is_error else "tool_end"
                        duration_ms = 0
                        if started_at and ended_at:
                            duration_ms = ended_at - started_at
                        self._activity_bubble_callback(
                            ActivityBubble(type=btype, session_key=sk, tool_name=item_name,
                                           duration_ms=duration_ms, icon=icon,
                                           status=ToolStatus.ERROR if is_error else ToolStatus.SUCCESS,
                                           agent_name=_agent_name)
                        )
            elif stream == "plan":
                # ── Activity bubble: plan update ───────────────────────────
                data = self._safe_data(payload)
                title = data.get("title", "") or ""
                steps_raw = data.get("steps", []) or []
                steps = [s.get("title", "") or str(s) for s in steps_raw]
                sk = payload.get("sessionKey", "") or session_key
                # SPEC-activity-drawer §2.4: resolve agent_name with the same
                # fallback chain used by the item/branch (PHASE 6). The plan,
                # approval, and patch branches are siblings of `item`, so they
                # need their own resolution — _agent_name is not in scope here.
                _agent_name = self._agent_name_for_event(payload)
                if title and self._activity_bubble_callback:
                    from models.activity import ActivityBubble, ToolStatus
                    bubble = ActivityBubble(type="plan", session_key=sk, icon="📋", title=title, steps=steps, agent_name=_agent_name)
                    self._activity_bubble_callback(bubble)
            elif stream == "approval":
                # ── Activity bubble: approval request ─────────────────────
                data = self._safe_data(payload)
                if data.get("phase") == "requested":
                    cmd = data.get("command", "") or ""
                    reason = data.get("reason", "") or ""
                    approval_id = data.get("approvalId", "") or ""
                    sk = payload.get("sessionKey", "") or session_key
                    # SPEC-activity-drawer §2.4: see plan/branch comment.
                    _agent_name = self._agent_name_for_event(payload)
                    if cmd and self._activity_bubble_callback:
                        from models.activity import ActivityBubble, ToolStatus
                        bubble = ActivityBubble(type="approval_request", session_key=sk, icon="🔒", command=cmd, reason=reason, approval_id=approval_id, agent_name=_agent_name)
                        self._activity_bubble_callback(bubble)
            elif stream == "patch":
                # ── Activity bubble: file edit summary ────────────────────
                data = self._safe_data(payload)
                if data.get("phase") == "end":
                    name = data.get("name", "") or ""
                    added = len(data.get("added", []) or [])
                    modified = len(data.get("modified", []) or [])
                    deleted = len(data.get("deleted", []) or [])
                    sk = payload.get("sessionKey", "") or session_key
                    # SPEC-activity-drawer §2.4: see plan/branch comment.
                    _agent_name = self._agent_name_for_event(payload)
                    if name and self._activity_bubble_callback:
                        from models.activity import ActivityBubble, ToolStatus
                        bubble = ActivityBubble(type="patch", session_key=sk, tool_name=name, added=added, modified=modified, deleted=deleted, icon="✏️", agent_name=_agent_name)
                        self._activity_bubble_callback(bubble)
            elif stream == "command_output":
                # ── Activity bubble: gateway exec result ───────────────────
                # Mirrors the local exec adapter in connection_sync_handler.py:225
                # but for gateway agents (Qaster, etc.) that run tools remotely.
                # Only handle phase=end; phase=delta streams are ignored (same
                # design as the patch branch — we render the final summary, not
                # the streaming chunks).
                data = self._safe_data(payload)
                if data.get("phase") == "end":
                    name = data.get("name", "") or ""
                    output = data.get("output", "") or ""
                    # BUGFIX-1 audit: exitCode may arrive as a string from
                    # JSON serialization edge cases. Coerce to int so "0"
                    # is treated as success (not error). `or 0` handles both
                    # None and 0 cleanly.
                    exit_code = int(data.get("exitCode", 0) or 0)
                    duration_ms = data.get("durationMs", 0)
                    command = data.get("title", "") or ""
                    sk = payload.get("sessionKey", "") or session_key
                    # SPEC-activity-drawer §2.4: see plan/branch comment.
                    _agent_name = self._agent_name_for_event(payload)
                    if name and self._activity_bubble_callback:
                        from models.activity import ActivityBubble, ToolStatus
                        # BUGFIX-1 audit: honor both exit_code AND status.
                        # Gateway may send status="failed" with exitCode=0
                        # (e.g. timeout, killed signal). Either signal means error.
                        is_error = exit_code != 0 or data.get("status") == "failed"
                        bubble = ActivityBubble(
                            type="command_output",
                            session_key=sk,
                            tool_name=name,
                            icon="💻",
                            command=command,
                            output=output,
                            exit_code=exit_code,
                            duration_ms=duration_ms,
                            status=ToolStatus.ERROR if is_error else ToolStatus.SUCCESS,
                            agent_name=_agent_name,
                        )
                        self._activity_bubble_callback(bubble)
        if event == "agent":
            # BUGFIX-4: State machine transitions only apply to lifecycle events.
            # Other stream types (item, plan, approval, patch, command_output)
            # should NOT trigger on_agent_start/end/error — they nest their
            # own status inside `data` (or lack a `phase` field entirely) and
            # would otherwise mis-fire the state machine if a future gateway
            # payload ever surfaced a top-level `phase` on a non-lifecycle event.
            stream = payload.get("stream", "")
            if stream == "lifecycle":
                phase = self._safe_data(payload).get("phase", "")
                if phase == "start":
                    self.on_agent_start(session_key, payload)
                elif phase == "end":
                    self.on_agent_end(session_key, payload)
                elif phase == "error":
                    self.on_agent_error(session_key)

        elif event == "chat":
            state = payload.get("state", "")
            if state == "delta":
                self.on_chat_delta(self._extract_chat_text(payload) or "", session_key)
            elif state == "final":
                self.on_chat_final(session_key)

        elif event == "tool_call":
            self.on_tool_use(payload.get("tool_name", "") or "", session_key, payload)

        elif event == "res":
            self.on_res_confirmed(session_key)

        # tick, health, presence, etc. — no state handler, progress only

    # ── Per-session progress helpers ───────────────────────────────────────

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

    def _on_preflight_timeout(self, session_key: str):
        """Called when 30s pre-flight timeout expires — revert to idle and clear progress."""
        self._send_initiated_timers.pop(session_key, None)
        if self._state == "sending" and self._is_ui_active(session_key):
            sk = self._active_session() or session_key
            self._reset_session_state(sk)
            self._set_state("idle", session_key)
        return False  # don't re-run

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
