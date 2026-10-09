# ui/handlers/agent_runtime_handler.py
# Phase 1.4 — Wires AgentRuntime into the develcakes UI as a special agent.
#
# Responsibility: Owns the AgentRuntime lifecycle + dispatches its callbacks
#                 to the chat render pipeline.
#                 Provides the add_special_agent() API for window.py to register
#                 special agents (e.g. "Coder") that run without a gateway.
#
# Thread safety: All GTK operations are dispatched via GLib.idle_add when
#               GLib is provided. AgentRuntime callbacks are already dispatched
#               by the runtime; this handler just routes them to the render layer.
#
# Owner: window.py (composition root) — instantiates, owns reference.

from __future__ import annotations

import copy
import json
import logging
import os
import re
import shutil
import threading
import time
from dataclasses import is_dataclass, replace
from datetime import datetime, timezone
from models.feed_card import FeedCardData, cap_stored_body
from utils import git_ops
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from gi.repository import GLib

logger = logging.getLogger(__name__)

from ui.agent_runtime.session import RuntimeSessionMixin
from ui.agent_runtime.turn_ui import RuntimeTurnUiMixin
from ui.agent_runtime.provider import RuntimeProviderMixin


def _null_lock():
    """A no-op lock stand-in usable as a context manager. Returned by
    ARH._project_lock_for when no ReviewHandler is wired (defensive path;
    normal operation always has the real per-project lock)."""
    import contextlib
    return contextlib.nullcontext()


class AgentRuntimeHandler(
    RuntimeSessionMixin,
    RuntimeTurnUiMixin,
    RuntimeProviderMixin,
):
    """
    Wires AgentRuntime into the develcakes UI.

    Provides add_special_agent() to register named agents that route through
    AgentRuntime instead of the gateway. Handles the callbacks from AgentRuntime
    and routes them to ChatRenderHandler for display.

    Args:
        main_content:       MainContent instance — for get_chat_box_for_session()
        chat_render_handler: ChatRenderHandler — for start/update/end_streaming
        GLib_module:        gi.repository.GLib or None
    """

    def __init__(
        self,
        main_content,          # MainContent
        chat_render_handler,  # ChatRenderHandler
        GLib_module: "GLib | None" = None,
        review_handler=None,   # ReviewHandler — optional, for Phase 1.5 review layer
    ):
        self._mc = main_content
        self._crh = chat_render_handler
        self._fh = None  # FeedHandler — set via set_feed_handler() (Phase D)
        self._GLib = GLib_module
        self._review_handler = review_handler

        # Phase 4 Part B (SPEC-UI-RESPONSIVENESS-2 §2.4): per-session prep
        # lock. Held by _prepare_turn_conversation (loop thread) and taken
        # non-blocking by set_active_project()'s eager reconcile (main
        # thread) so no reader ever observes a half-prepared conversation.
        self._prep_locks: dict[str, "threading.Lock"] = {}
        self._prep_locks_guard = threading.Lock()

        # SPEC-09 SP2: one WorktreeManager per active project (seam:
        # set_active_project -> self._active_project; managers cached here).
        self._worktree_managers: dict[str, Any] = {}

        # SPEC-09 SP3: stop-all in-flight flag. Set True across the whole
        # stop_all_agents() aggregation; ReviewHandler reads it via
        # stop_all_in_progress() BEFORE git_ops.commit and aborts instead.
        self._stop_all_in_progress: bool = False
        # Review checkpoints aborted during the CURRENT stop-all window
        # (ReviewHandler increments via note_stop_all_aborted(); the summary
        # card consumes and resets the counter).
        self._stop_all_aborted_checkpoints: int = 0

        # Shared routing table — set via set_agent_routing() (maps session_key → project_name)
        # Used to route special agent responses to project chat boxes when no direct tab exists.
        self._agent_to_project = None

        # Registered agents: session_key → SpecialAgentDef (full definition)
        self._agents: dict[str, Any] = {}
        # Active project: (name, path) or None
        self._active_project: tuple[str, str] | None = None

        # SPEC-09 SP2: one WorktreeManager per project path (created lazily;
        # a non-git project degrades to a disabled manager). See
        # _worktree_for_turn for the consumption site.
        # name → AgentRuntime instance (one rt per agent for isolation)
        self._runtimes: dict[str, Any] = {}
        # Tool call → feed card ID mapping: session_key → card_id
        # Used by Phase D to update feed cards when tool calls complete.
        # Keyed by session_key only — tool_name stored in card metadata.
        self._tool_card_ids: dict[str, str] = {}
        # Accumulated streaming text: session_key → cumulative text
        # AgentRuntime sends incremental deltas; ChatRenderHandler expects cumulative.
        self._streaming_text: dict[str, str] = {}
        # UI responsiveness throttle: session_key → last monotonic time we dispatched
        # update_streaming from _do_text_delta. Text is ALWAYS accumulated; rendering is
        # throttled to at most 20 calls/sec per session.
        self._last_delta_dispatch: dict[str, float] = {}
        self._delta_throttle_sec = 0.05  # 50ms — at most 20 throttle-pass updates/sec
        # AC3 Part A — producer-side coalescing state.
        # _delta_dispatch_pending: session_keys with a dispatch already queued
        # on the main loop. Added by the producer thread, cleared in
        # _do_text_delta's finally (main thread); add/discard on a set are
        # atomic under the GIL, so no lock is needed.
        self._delta_dispatch_pending: set[str] = set()
        # _delta_dirty: session_keys whose latest accumulated text is NOT
        # covered by an already-queued dispatch (producer suppressed by
        # pending/throttle). Consumed by _do_text_delta's finally, which then
        # schedules exactly one unconditional trailing dispatch.
        self._delta_dirty: set[str] = set()
        # Pending approval cards: approval_id (card_id) → {session_key, tool_name, args}
        # Used by Phase E to resolve approvals when PM clicks Approve/Deny.
        self._pending_approvals: dict[str, dict] = {}

        self._on_agent_start_cb: Callable[[str], None] | None = None
        self._on_agent_end_cb: Callable[[str], None] | None = None
        # SPEC-16 SP2: pill. Fired from _on_text_delta with the new slice only.
        self._on_stream_delta_cb: Callable[[str, str], None] | None = None
        # SPEC-16 SP2: pill. Fired from _do_tool_call_start after the ended-session guard.
        self._on_tool_start_cb: Callable[[str, str], None] | None = None
        self._on_agent_response: Callable[[str, str, str | None], None] | None = None  # Phase 6.2
        # SPEC-activity-drawer Phase 1: command_output callback.
        # cb(session_key, command, output, exit_code, duration_ms) — drawer uses
        # command for the row label, output for click-to-expand revealer, and
        # exit_code + duration_ms for the exit badge and duration display
        # (per SPEC-activity-drawer §2.5).
        self._on_command_output: Callable[[str, str, str, int, int], None] | None = None
        # NEW: activity-bubble callback for the drawer (tool lifecycle).
        # cb(ActivityBubble) — fired on tool_start/tool_end/patch.
        # Phase 4 Part C: deliveries are BATCHED per session behind a 250 ms
        # flush (see _emit_activity_bubble) instead of one dispatch per tool
        # event. With no GLib main loop (tests / direct callers) the bubbles
        # are delivered inline, matching this handler's GLib dispatch idiom.
        self._on_activity_bubble: Callable | None = None
        # Phase 4 Part C (spec §2.4): per-session pending-bubble queue +
        # one-shot flush timer, mirroring the 250 ms cadence of
        # ActivityHandler._status_tick. Order is FIFO per session; bubbles are
        # never dropped and never reordered.
        self._bubble_queue: dict[str, list] = {}
        self._bubble_timers: dict[str, int] = {}
        self._bubble_lock = threading.Lock()
        self.ACTIVITY_BUBBLE_FLUSH_MS = 250
        # NEW: drawer-lifecycle callback for agent start/end separators.
        # cb(session_key, agent_name, phase) where phase ∈ {"start", "end"}.
        self._on_drawer_lifecycle: Callable | None = None
        # NEW: pending tool args for patch path enrichment.
        # session_key → dict of args from _do_tool_call_start.
        self._pending_tool_args: dict[str, dict] = {}
        # BUG #2: Track sessions that have ended (cancel/error/complete) to prevent
        # orphan tool_start bubbles from stale idle_add dispatches.
        self._ended_sessions: set[str] = set()
        # RACE-FIX v4: per-session completion flag. Set at the TOP of
        # _do_response_complete and _do_error (main thread). Prevents
        # duplicate completion from rendering two bubbles. Cleared by
        # send_to_special_agent on the next turn.
        self._session_completed: set[str] = set()
        # RACE-FIX v4: per-session turn token. A unique object assigned in
        # send_to_special_agent. Both deltas and completion capture it.
        # A delta with a mismatched token (from a previous turn) is dropped.
        # Unlike a counter, the token does NOT change at completion time,
        # so same-turn deltas are never wrongly dropped.
        self._turn_tokens: dict[str, object] = {}

        # SPEC-12 (R5 REV 3): turn-scoped reply target. session_key → the
        # DISPLAY key the CURRENT turn's reply renders under. Set on ENTRY of
        # every send_to_special_agent (reply_target or the routing key);
        # consumed by _reply_key() at the 11 render/mount sites; cleared at
        # the END of _do_response_complete/_do_error (SP3b). A per-send slot
        # (never a session-scoped mark) so a member's private /ask reply does
        # not mute its LATER group replies.
        self._turn_reply_target: dict[str, str] = {}

        # SPEC-10 SP2b (D2 REV 2): dispatch-time attribution snapshot for the
        # turn-complete agent checkpoint: session_key ->
        # (project_name, project_path, write_cwd). None until
        # _prepare_turn_conversation resolves the write cwd (worktree or
        # root); read at completion via the token-freshness guard. Bounded by
        # overwrite-on-dispatch discipline (one entry per agent session).
        self._turn_attr: dict[str, tuple[str, str, str]] = {}
        # SPEC-10 D8d: per-session checkpoint serialization. Two same-session
        # turns can overlap (turn N's checkpoint daemon still running when
        # N+1 completes); both write ONE worktree. Serializes the whole
        # checkpoint body; no eviction (session keys are roster-bounded).
        self._checkpoint_locks: dict[str, threading.Lock] = {}
        # V2 exec auto-accept callback (Phase 6): returns current exec mode
        # ("off" | "show" | "silent") or None. Set by window.py wiring via
        # set_check_exec_auto_accept_callback(). When the callback returns
        # "silent", _do_approval_needed bypasses card creation and approves
        # directly via runtime.approve_exec(). See SPEC-AUTO-ACCEPT-GRANULAR-1
        # §2.5 (Silent bypass, BUG #11 fix) and GRANULAR-PHASE-6-INSTRUCTIONS.md
        # Sub-change A.
        self._on_check_exec_auto_accept: Callable[[], str | None] | None = None
        # Per-session pending exec_command text — captured in _do_tool_call_start,
        # consumed in _do_tool_call_result. Keyed by session_key (one in-flight
        # exec per session is the realistic case).
        self._pending_exec_commands: dict[str, str] = {}

        # Per-session token usage cache: session_key → (total_tokens, total_cost)
        # Populated by _on_token_usage, read by get_session_usage().
        self._session_usage: dict[str, tuple[int, float]] = {}
        # SSE hardening Phase 1: capture exception objects from _on_error
        # so _do_error can enrich the displayed message with provider/model context.
        self._last_error_exception: dict[str, "BaseException | None"] = {}
        # SPEC-19 SP4: window-registered live-bridge Promise resolver (late-bound).
        self._live_bridge_resolver = None

        # Phase A — Context UI state.
        # _last_breakdown: session_key → most recent breakdown dict.
        # _last_warning_pct: session_key → last usage_pct at which we warned.
        # _first_compaction_seen: session_key → bool, true after first
        #   compaction bubble fired for this session (anti-spam).
        # _on_token_breakdown_extra: optional extra listener for breakdown
        #   events (used by the context meter in window.py).
        self._last_breakdown: dict[str, dict] = {}
        self._last_warning_pct: dict[str, float] = {}
        self._first_compaction_seen: dict[str, bool] = {}
        self._on_token_breakdown_extra: Callable | None = None

    def set_on_agent_start(self, cb: Callable[[str], None]) -> None:
        """Set callback fired when a local agent starts processing. Signature: cb(session_key)."""
        self._on_agent_start_cb = cb

    def set_on_agent_end(self, cb: Callable[[str], None]) -> None:
        """Set callback fired when a local agent finishes processing. Signature: cb(session_key)."""
        self._on_agent_end_cb = cb

    def set_on_stream_delta(self, cb: Callable[[str, str], None]) -> None:
        """cb(session_key, delta_text). delta_text is the new slice only.

        Trigger: _on_text_delta, after the slice is appended to _streaming_text
        and after the ended-session and stale-token guards. Empty text does
        not fire. A raising callback is logged and does not drop the render.
        """
        self._on_stream_delta_cb = cb

    def set_on_tool_start(self, cb: Callable[[str, str], None]) -> None:
        """cb(session_key, tool_name).

        Trigger: _do_tool_call_start, after the ended-session return, next to
        the drawer tool_start bubble. Fires with no project open. A raising
        callback is logged and does not drop the bubble.
        """
        self._on_tool_start_cb = cb

    def set_on_agent_response(self, cb: Callable[[str, str, str | None], None]) -> None:
        """Set callback for agent response command parsing hook (Phase 6.2).

        Called after an agent's final response is rendered, with the agent's
        session key, full response text, and active project name.
        """
        self._on_agent_response = cb

    def set_on_command_output(self, cb: Callable[[str, str, str, int, int], None]) -> None:
        """Set callback for command_output drawer events (SPEC-activity-drawer §2.5).

        cb(session_key, command, output, exit_code, duration_ms) — fired when
        an exec_command completes.
        - `command` is the shell command string captured at start time.
        - `output` is the last 10 lines of stdout/stderr from the ToolResult.
        - `exit_code` is the int exit code from the ToolResult (0 if not set).
        - `duration_ms` is the int tool execution time in ms.

        Wired in connection_sync_handler.sync() to the drawer's append_event
        with a dict constructed from these five arguments.
        """
        self._on_command_output = cb

    def set_on_activity_bubble(self, cb) -> None:
        """Set callback for local tool lifecycle → activity drawer.

        cb(ActivityBubble) — fired on tool_start/tool_end/patch.

        Phase 4 Part C: the callback is invoked from the per-session BATCHED
        flush (`_flush_activity_bubbles`, driven by a 250 ms GLib timeout
        armed by `_emit_activity_bubble`) — or inline when no GLib main loop
        is available. Bubbles arrive in emission order, at most one batch per
        250 ms per session.
        """
        self._on_activity_bubble = cb

    def _emit_activity_bubble(self, bubble) -> None:
        """Queue a local tool-lifecycle bubble for the batched flush.

        Phase 4 Part C (SPEC-UI-RESPONSIVENESS-2 §2.4): instead of one
        dispatch per tool start/result event, bubbles are appended to a
        per-session FIFO queue and delivered by a single 250 ms flush — the
        same cadence ActivityHandler._status_tick uses. Bubbles are never
        dropped or reordered.

        Falls back to an immediate delivery when no GLib main loop is present
        (tests, direct callers) — the same dual-mode dispatch this file uses
        everywhere else.
        """
        if self._on_activity_bubble is None:
            return
        session_key = bubble.session_key
        if self._GLib is None:
            self._on_activity_bubble(bubble)
            return
        with self._bubble_lock:
            self._bubble_queue.setdefault(session_key, []).append(bubble)
            if session_key not in self._bubble_timers:
                self._bubble_timers[session_key] = self._GLib.timeout_add(
                    self.ACTIVITY_BUBBLE_FLUSH_MS,
                    self._flush_activity_bubbles,
                    session_key,
                )

    def _flush_activity_bubbles(self, session_key: str) -> bool:
        """Deliver every queued bubble for one session, in order.

        GLib timeout callback: returns False so the one-shot timer does not
        repeat. A new enqueue after this returns arms a fresh timer.
        """
        with self._bubble_lock:
            self._bubble_timers.pop(session_key, None)
            pending = self._bubble_queue.pop(session_key, [])
        cb = self._on_activity_bubble
        if cb is None:
            return False
        for bubble in pending:
            cb(bubble)
        return False

    def flush_pending_activity_bubbles(self, session_key: str) -> None:
        """Deliver a session's queued bubbles NOW, cancelling its timer.

        Used at turn end (before the drawer's lifecycle "end" separator) so
        the separator can never be rendered above a tool row that logically
        precedes it.
        """
        with self._bubble_lock:
            timer_id = self._bubble_timers.pop(session_key, None)
            pending = self._bubble_queue.pop(session_key, [])
        if timer_id is not None and self._GLib is not None:
            try:
                self._GLib.source_remove(timer_id)
            except Exception:
                # A timer that already fired is fine — the queue is what
                # matters; report nothing worse than a stale-source warning.
                logger.debug(
                    "flush_pending_activity_bubbles: source %r already gone", timer_id
                )
        cb = self._on_activity_bubble
        if cb is None or not pending:
            return
        for bubble in pending:
            cb(bubble)

    def set_on_drawer_lifecycle(self, cb) -> None:
        """Set callback for agent turn → drawer separators.

        cb(session_key, agent_name, "start"|"end") — fired when a local
        agent starts or ends its turn.
        """
        self._on_drawer_lifecycle = cb

    def set_check_exec_auto_accept_callback(
        self, callback: Callable[[], str | None] | None
    ) -> None:
        """Install callback that returns the current exec auto-accept mode,
        or None if exec auto-accept is off. (Phase 6 / v2)

        Per SPEC-AUTO-ACCEPT-GRANULAR-1 §2.5 + GRANULAR-PHASE-6-INSTRUCTIONS
        Sub-change A: AgentRuntimeHandler does NOT import FeedHandler
        (§8.6 R2 no handler-to-handler imports). Instead, window.py wires
        FeedHandler.get_exec_auto_accept_mode as the callback. When the
        callback returns "silent", _do_approval_needed bypasses card
        creation and approves directly via runtime.approve_exec().

        The callback signature is `() -> str | None`:
          - returns "off" | "show" | "silent" to indicate exec mode
          - returns None if FeedHandler's _prefs is not yet initialized
            (gracefully degrades to no-bypass — card is created normally)

        Trigger: invoked at the top of _do_approval_needed() on every
        approval request. The callback must be cheap (called once per
        approval); FeedHandler.get_exec_auto_accept_mode is a single
        attribute read on _prefs.exec_command.mode.
        """
        self._on_check_exec_auto_accept = callback

    def set_review_handler(self, review_handler) -> None:
        """Set ReviewHandler after construction (deferred to avoid circular deps with window._build)."""
        self._review_handler = review_handler

    def stop_all_agents(self) -> dict[str, str]:
        """Stop every agent: cancel turns, kill registered processes, abort
        in-flight review checkpoints. Merged sk → outcome across runtimes.

        SPEC-09 SP3 aggregation seam (toolbar → here via window). Sets the
        stop flag BEFORE iterating so a review checkpoint starting mid-loop
        still sees it, clears it in finally; emits ONE summary feed card
        (sessions cancelled, approvals denied, processes killed, checkpoints
        aborted). The no-op case ("0 turns in flight") is carded too
        (spec §7). Returns the merged outcome dict.
        """
        self._stop_all_in_progress = True
        try:
            merged: dict[str, str] = {}
            for rt in list(self._runtimes.values()):
                try:
                    for sk, outcome in rt.stop_all().items():
                        merged[sk] = outcome
                except Exception:
                    logger.exception("stop_all_agents: runtime stop_all raised")
            # Review checkpoints that aborted under the flag report through
            # this counter (ReviewHandler increments via note_aborted).
            aborted = self._stop_all_aborted_checkpoints
            self._stop_all_aborted_checkpoints = 0
            self._emit_stop_all_card(merged, aborted)
            return merged
        finally:
            self._stop_all_in_progress = False

    def stop_all_in_progress(self) -> bool:
        """True while stop-all is running. Review checkpoints read this
        BEFORE git_ops.commit and abort instead (no commit under stop-all)."""
        return self._stop_all_in_progress

    def note_stop_all_aborted(self) -> None:
        """ReviewHandler calls this when it aborts a checkpoint under the
        stop-all flag; the count feeds the stop-all summary card."""
        self._stop_all_aborted_checkpoints += 1

    def _emit_stop_all_card(self, outcomes: dict[str, str], aborted_checkpoints: int) -> None:
        """One summary card for a completed stop-all (spec §7). No-op case
        (empty outcomes) still cards: "0 turns in flight"."""
        if self._fh is None:
            logger.info("stop-all card skipped: no feed handler wired")
            return
        cancelled = sum(1 for sk, o in outcomes.items() if sk != "*" and o.startswith("cancelled"))
        denied = sum(
            int(p.split(":")[1]) for o in outcomes.values() for p in o.split("+")
            if p.startswith("denied:") and p.split(":")[1].isdigit()
        )
        killed = sum(
            int(p.split(":")[1]) for o in outcomes.values() for p in o.split("+")
            if p.startswith("killed:") and p.split(":")[1].isdigit()
        )
        # SP3 fix round (BUG#3): groups that SURVIVED SIGKILL surface as
        # "N-unkillable" — a hostile-process situation the PM must SEE,
        # never a silent success count.
        unkillable = sum(
            int(p.split("-")[0]) for o in outcomes.values() for p in o.split("+")
            if p.endswith("-unkillable") and p.split("-")[0].isdigit()
        )
        lines = [f"Turns cancelled: {cancelled}"]
        if denied:
            lines.append(f"Approvals denied: {denied}")
        if killed:
            lines.append(f"Processes killed: {killed}")
        if unkillable:
            lines.append(
                f"Processes UNKILLABLE: {unkillable} (survived SIGKILL — "
                f"check with ps/kill manually)"
            )
        if aborted_checkpoints:
            lines.append(f"Checkpoints aborted: {aborted_checkpoints}")
        if not outcomes and not aborted_checkpoints:
            lines = ["0 turns in flight — nothing to stop"]
        from models.feed_card import FeedCardData
        card = FeedCardData(
            card_type="system",
            source="system",
            title="■ Stop All",
            body="\n".join(lines),
            author="Stop All",
            timestamp=datetime.now(timezone.utc),
            project_name=self._active_project[0] if self._active_project else "(none)",
            metadata={"origin": "stop-all", "sessions": len(outcomes)},
        )
        self._fh.add_card(card)

    def set_feed_handler(self, feed_handler) -> None:
        """Set FeedHandler. Called by window.py during _build (Phase D)."""
        self._fh = feed_handler

    def set_agent_routing(self, routing_table) -> None:
        """Set AgentRoutingTable. Called by window.py during _build.
        Used to route special agent responses to project chat boxes."""
        self._agent_to_project = routing_table

    def set_active_project(self, project_name: str, project_path: str) -> None:
        """
        Set the active project for all special agents.

        Called by window.py when a project tab opens:
          self._project_handler.set_on_project_opened(
              lambda n, p: self._agent_runtime_handler.set_active_project(n, p)
          )

        This injects project_path into all existing (hot) conversations and
        ensures new conversations get the correct project context.

        Cold agents (those that have never been instantiated in this session)
        are NOT updated here — their conversations are loaded from disk on
        first send, and the lazy reconciliation in
        `AgentRuntime._rebuild_conversation_context` fires at that point.
        See option-C+ design in
        `.crabcakes/feed.json` / the production debugger incident: the
        original bug was a cold agent seeing a stale project_path; the fix
        is to rebuild on first send, against the currently-active project.

        HIGH-5 (Phase 6): If the project has a `.crabcakes/` directory with
        rule/bug files AND is not yet trusted, show a confirmation dialog
        before injecting its content. The dialog is shown asynchronously via
        GLib.idle_add (we're called from a tab-open callback that may not be
        on the GTK main thread).
        """
        self._active_project = (project_name, project_path)

        # HIGH-5: Schedule a trust check + dialog on the main thread, BEFORE
        # we rebuild system prompts. The dialog blocks visually but doesn't
        # block the call site; subsequent prompts will pull fresh state.
        if self._GLib is not None:
            self._GLib.idle_add(self._maybe_prompt_project_trust, project_name, project_path)
        else:
            # No GLib (tests): skip the dialog and rely on the trust store.
            # If the project isn't trusted, compose_system_prompt will skip
            # the .crabcakes/ files anyway (fail-secure default).
            pass

        # Update project_path on all existing conversations.
        # For hot agents (already in memory) this updates the in-memory
        # Conversation directly. For cold agents (loaded from disk only when
        # the user sends a message) we leave the on-disk project_path in
        # place — the lazy reconciliation in _rebuild_conversation_context
        # fires the next time the agent is loaded for a send. The lazy
        # path always wins because it knows the current active project.
        for sk, agent_def in self._agents.items():
            rt = self._runtimes.get(agent_def.display_name)
            if rt is None:
                continue
            conv = rt.get_conversation(sk)
            if conv is None:
                continue  # Cold agent — lazy path handles it on next send.
            if conv.project_path != project_path:
                # Phase 4 Part B: a send for this session may have its
                # preparation in flight on the loop thread. Take the prep
                # lock NON-BLOCKING — the UI must never wait on disk I/O —
                # and skip the eager rebuild when it is held: the prep runs
                # to completion against the project that was active when the
                # user hit send, and the lazy reconciliation on the next
                # send applies the newly-active project.
                lock = self._prep_lock(sk)
                if not lock.acquire(blocking=False):
                    logger.info(
                        "set_active_project: prep in flight for %s — skipping eager "
                        "reconcile; next send's lazy path applies %s",
                        sk, project_path,
                    )
                    continue
                try:
                    rt._rebuild_conversation_context(
                        sk,
                        project_path,
                        agent_role=agent_def.role,
                    )
                finally:
                    lock.release()
        logger.info("AgentRuntimeHandler: active project set to %s (%s)", project_name, project_path)

    def _maybe_prompt_project_trust(self, project_name: str, project_path: str) -> None:
        """HIGH-5: Show a confirmation dialog if the project has .crabcakes/
        content that hasn't been trusted yet. Runs on the GTK main thread
        (scheduled via GLib.idle_add)."""
        from utils.project_trust import (
            has_crabcakes_content,
            is_project_trusted,
            trust_project,
        )
        if not has_crabcakes_content(project_path):
            return  # nothing to gate
        if is_project_trusted(project_path):
            return  # already trusted

        try:
            import gi
            gi.require_version("Gtk", "4.0")
            from gi.repository import Gtk
        except (ImportError, ValueError):
            logger.warning("HIGH-5: Gtk not available; skipping trust dialog")
            return

        dialog = Gtk.MessageDialog(
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text=f"Trust project “{project_name}”?",
        )
        dialog.set_property(
            "secondary-text",
            "This project contains a .crabcakes/ directory with rules and "
            "bug-journal entries. These will be injected into every agent's "
            "system prompt for this project. Only approve if you trust the "
            "project's contents — untrusted project content can attempt to "
            "manipulate agent behavior.",
        )

        def on_response(_dialog, response_id):
            try:
                if response_id == Gtk.ResponseType.YES:
                    trust_project(project_path, reason="user-approved-via-dialog")
                    logger.info("HIGH-5: user trusted project %s via dialog", project_path)
                else:
                    logger.info("HIGH-5: user declined trust for project %s", project_path)
            finally:
                _dialog.close()

        dialog.connect("response", on_response)
        dialog.show()

    def clear_active_project(self) -> None:
        """
        Clear the active project. Called by window.py when the project tab closes.

          self._project_handler.set_on_project_closed(
              lambda name: self._agent_runtime_handler.clear_active_project()
          )
        """
        self._active_project = None
        logger.info("AgentRuntimeHandler: active project cleared")

    # ── CLI nudge surface (AGENTCTRL1 Phase 2 — SPEC-AGENT-CONTROL-1 §3) ────
    #
    # Small public methods so the entry point (main.py) never reads handler
    # privates and never imports ui.handlers — the window object carries the
    # handler graph (duck-typed at the entry-point boundary; the composition
    # root ui/window.py wires the real handler, as it does for chat_handler).

    def special_agent_session_exists(self, session_key: str) -> bool:
        """
        True when a CLI nudge to ``session_key`` may be delivered (§3.3).

        A session "exists" only when the special agent is registered AND the
        handler has an active project: special agents are project-scoped, and
        send_to_special_agent() refuses without one ("Open a project first").
        Folding the project check in here keeps the nudge path honest — a
        nudge without a project would otherwise "deliver" straight into an
        error path.
        """
        if session_key not in self._agents:
            return False
        return self._active_project is not None

    def special_agent_turn_state(self, session_key: str) -> Any | None:
        """
        Turn state for ``session_key`` (TurnStatus or None) — read-only.

        Reads ONLY an already-constructed runtime (never _get_runtime: a
        nudge must not spawn agent threads just to inspect state). None when
        the agent has no runtime yet (nothing in flight).
        """
        agent_def = self._agents.get(session_key)
        if agent_def is None:
            return None
        rt = self._runtimes.get(agent_def.display_name)
        if rt is None:
            return None
        return rt.get_turn_state(session_key)

    def publish_cli_nudge_card(self, session_key: str, text: str) -> str | None:
        """
        Publish the CLI-nudge feed card (§3.3 artefact — always accompanies a
        delivered nudge).

        Visibly distinct from PM-typed input: metadata origin='cli-nudge'
        plus a '[via CLI]' body marker, so the PM can tell at a glance which
        turns they authored and which arrived through the CLI channel.

        Returns the card_id, or None when no feed handler is wired.
        """
        if self._fh is None:
            logger.warning("publish_cli_nudge_card: no feed handler wired; card skipped")
            return None
        agent_def = self._agents.get(session_key)
        agent_name = agent_def.display_name if agent_def else "Agent"
        project_name = self._active_project[0] if self._active_project else "(none)"
        from models.feed_card import FeedCardData
        card = FeedCardData(
            card_type="agent_action",
            source="agent",
            title=f"CLI nudge to {agent_name}",
            body=f"[via CLI] {text}",
            author="CLI",
            timestamp=datetime.now(timezone.utc),
            project_name=project_name,
            metadata={
                "origin": "cli-nudge",
                "session_key": session_key,
                "chars": len(text),
            },
        )
        card_id = self._fh.add_card(card)
        logger.info("CLI nudge card published for %s (card_id=%s)", session_key, card_id)
        return card_id

    # ── Special agent registration ──────────────────────────────────────────

    def add_special_agent(self, agent_def: Any) -> None:
        """
        Register a special agent backed by AgentRuntime.

        Args:
            agent_def: SpecialAgentDef — the full agent definition.

        The agent appears in the agents list via set_special_agents() in window.py.
        """
        self._agents[agent_def.conv_id_prefix] = agent_def
        logger.info("Registered special agent: %s (%s)", agent_def.display_name, agent_def.conv_id_prefix)

    def get_special_agents(self) -> dict[str, str]:
        """Return {session_key: display_name} for registered special agents."""
        return {sk: ad.display_name for sk, ad in self._agents.items()}



    def get_special_agent_def(self, session_key: str) -> Any | None:
        """Return the SpecialAgentDef for a session key, or None."""
        return self._agents.get(session_key)

    def get_agent_name_for_session(self, session_key: str) -> str:
        """Return the display name of the local special agent that owns this session, or ''.

        Used by the local exec adapter (in activity_wiring_handler.py) to populate
        ActivityBubble.agent_name so the activity drawer shows the right agent name
        in the [Agent] column. Mirrors the fallback chain in
        ActivityHandler._resolve_agent_name, but resolves locally via session_key
        since local exec bubbles don't have a gateway payload to read data.agentName
        from.

        Args:
            session_key: The agent's session key.

        Returns:
            The agent's display name (e.g. "Coder"), or "" if not found.
        """
        agent_def = self._agents.get(session_key)
        if agent_def is None:
            return ""
        return getattr(agent_def, "display_name", "") or ""

    def approve_exec(self, approval_id: str, approved: bool) -> None:
        """
        Resolve a pending exec_command approval.

        Called when the PM clicks Approve or Deny on a pending-approval feed card.
        approval_id is the card_id of the approval card.

        The handler-level approval_id (card_id) maps to the runtime's
        (session_key, tool_name, args) via self._pending_approvals.
        """
        pending = self._pending_approvals.pop(approval_id, None)
        if pending is None:
            logger.warning("approve_exec: no pending approval for %s", approval_id)
            return

        session_key = pending["session_key"]
        tool_name = pending["tool_name"]
        args = pending["args"]

        # SPEC-19 SP4 Part C: a live-bridge approval resolves the page's
        # Promise (the runtime forward below is a no-op for bridge calls —
        # no runtime owns "special:live-bridge"). The window registers the
        # resolver seam; late-bound so ARH never imports the render handler.
        bridge_call_id = pending.get("live_bridge_call_id")

        # Find the runtime that owns this session and forward the approval
        for name, rt in self._runtimes.items():
            if rt.get_conversation(session_key) is not None:
                rt.approve_exec(session_key, tool_name, args, approved)
                break

        # Update the card status in the feed
        if self._fh is not None:
            card = self._fh.get_card(approval_id)
            if card is not None:
                # Build the RESOLVED copy and let update_card own the in-memory
                # replacement: mutating the live card first would leave it
                # looking resolved while a no-op persist left disk pending —
                # the UI would claim success and the approval would reappear
                # on reload. (Audit: mutate-before-persist, same class as the
                # review-bar resolution fix.)
                new_metadata = dict(card.metadata or {})
                new_metadata["status"] = "approved" if approved else "denied"
                resolved = replace(card, metadata=new_metadata, accepted=approved)
                self._fh.update_card(approval_id, resolved)

        # SPEC-19 SP4: resolve the live-bridge Promise if this was a bridge card
        if bridge_call_id and self._live_bridge_resolver is not None:
            try:
                self._live_bridge_resolver(bridge_call_id, approved)
            except Exception:
                logger.exception("live-bridge resolver raised for %s", bridge_call_id)

    def set_live_bridge_resolver(self, cb) -> None:
        """SPEC-19 SP4: window registers the callback that resolves a live
        page's pending Promise when its approval card is answered. cb(call_id,
        approved: bool). Late-bound; ARH never imports the render handler."""
        self._live_bridge_resolver = cb

    # ── AgentRuntime lifecycle ────────────────────────────────────────────────


    def _get_runtime(self, name: str, agent_def=None) -> Any:
        """
        Get or create the AgentRuntime for a named agent.

        Each named agent gets its own AgentRuntime instance to keep
        conversations isolated.

        Args:
            name: Display name of the agent.
            agent_def: Optional SpecialAgentDef. If provided, the agent's
                      llm_name overrides the global default_provider.
        """
        if name in self._runtimes:
            return self._runtimes[name]

        from agent.config import load_agent_config
        from agent.runtime import AgentRuntime

        config = load_agent_config()

        # If the agent definition specifies a provider, use it as the default
        if agent_def is not None and getattr(agent_def, 'llm_name', None):
            llm_name = agent_def.llm_name
            if llm_name in config.providers:
                config.default_provider = llm_name
                provider = config.providers[llm_name]
            else:
                provider = config.providers.get(config.default_provider)
        else:
            provider = config.providers.get(config.default_provider)

        if not provider:
            raise RuntimeError("No provider configured — add one in Settings → Providers.")

        if not provider.api_key:
            raise RuntimeError(f"No API key configured for provider {provider.name}")

        rt = AgentRuntime(
            config=config,
            GLib=self._GLib,
            on_text_delta=self._on_text_delta,
            on_turn_start=self._on_turn_start,
            on_tool_call_start=self._on_tool_call_start,
            on_tool_call_result=self._on_tool_call_result,
            on_tool_call_approval_needed=self._on_tool_call_approval_needed,
            on_response_complete=self._on_response_complete,
            on_token_usage=self._on_token_usage,
            on_token_breakdown=self._on_token_breakdown,
            on_error=self._on_error,
            on_enforcement_status=self._on_enforcement_status,
        )
        # SPEC-08 SP4A (Ruling 2: handler-owned card): the runtime ships a
        # logged fallback banner receiver; this handler owns project_name +
        # the feed seam, so it builds the FeedCardData itself. Registered on
        # every runtime (the process-latch makes the sweep once-only; later
        # registrations are inert overrides of an already-fired window).
        rt.set_on_store_migration(
            on_complete=self._on_store_migration_complete,
            on_progress=self._on_store_migration_progress,
        )
        rt.start()
        self._runtimes[name] = rt
        logger.info("Created AgentRuntime for special agent: %s", name)
        return rt



    # ── Public: send a message to a special agent ────────────────────────────


    def _prep_lock(self, session_key: str) -> "threading.Lock":
        """Return the per-session prep lock, creating it on first use.

        Phase 4 Part B (SPEC-UI-RESPONSIVENESS-2 §2.4). See
        _prepare_turn_conversation for the locking rationale.
        """
        with self._prep_locks_guard:
            lock = self._prep_locks.get(session_key)
            if lock is None:
                lock = self._prep_locks[session_key] = threading.Lock()
            return lock

    def _worktree_for_turn(
        self,
        session_key: str,
        agent_def: Any,
        project_path: str | None,
    ) -> str | None:
        """SPEC-09 SP2: the worktree cwd for this turn, or None.

        Writer (agent_def.can_write) + a LIVE SP1 lease held by this session
        -> ensure + return the worktree path under the active project.
        Non-writer, no lease, expired lease, non-git project, or ANY worktree
        failure -> None (the turn runs in the project root; the worktree is
        an optimization with safety rails, never a hard dependency).

        Lease-expiry reset (v3 MED): a writer WITHOUT a live lease whose
        current conv.project_path is a worktree is handled by the caller
        (this method only decides the NEW path).
        """
        if not getattr(agent_def, "can_write", False):
            return None
        if not project_path:
            return None
        try:
            # Deferred import: utils must not import ui (architecture rule);
            # this helper is the ui-side consumer.
            from utils.work_persistence import find_live_lease

            if find_live_lease(project_path, session_key) is None:
                return None
            from utils.worktree_manager import WorktreeManager

            manager = self._worktree_manager_for(project_path)
            worktree_id = WorktreeManager.worktree_id_for_session(session_key)
            return manager.ensure_worktree(worktree_id)
        except Exception as e:  # noqa: BLE001 — a worktree failure must
            # never break the turn; the agent falls back to the project root.
            logger.warning(
                "worktree resolve failed for %s in %s: %s",
                session_key, project_path, e,
            )
            return None

    def _worktree_manager_for(self, project_path: str):
        """A WorktreeManager per project path, cached on ARH (managers are
        cheap but not free; a git-repo probe per turn would be wasteful).
        A non-git project yields a DISABLED manager (v3): the ctor degrades
        instead of raising (probe N item 2)."""
        cached = self._worktree_managers.get(project_path)
        if cached is None:
            from utils.worktree_manager import WorktreeManager

            cached = WorktreeManager(project_path)
            self._worktree_managers[project_path] = cached
        return cached

    def _prepare_turn_conversation(
        self,
        *,
        rt,
        session_key: str,
        agent_def: Any,
        project_path: str | None,
        project_name: str | None,
        agent_model: str | None,
        si_enforcement: bool | None,
        turn_token: object,
    ) -> None:
        """Prepare a conversation for a turn — runs on the RUNTIME LOOP thread.

        Phase 4 Part B (SPEC-UI-RESPONSIVENESS-2 §2.4). Moved verbatim out of
        send_to_special_agent, which used to run it inline on the GTK main
        thread (~300–500 ms per send):
          * load the persisted conversation from disk (I/O + deserialize),
          * reconcile the project context (`_rebuild_conversation_context`),
          * create the conversation when none exists,
          * sync per-agent state (api_key / model / app_title / fallback
            provider / role / MCP list / SI enforcement),
          * reset step_count for the new task.

        Concurrency contract (the spec flags this MEDIUM-HIGH):
          * The per-session prep lock serializes two concurrent sends for the
            same session, so neither can observe a half-prepared conversation.
            set_active_project()'s eager reconcile takes the same lock
            non-blocking and skips when a prep is in flight.
          * The RACE-FIX v4 turn token guards against a SUPERSEDED send
            clobbering a live conversation: if a newer send already rotated the
            token, this prep performs no mutation. Its own loop then fails the
            conversation lookup and terminates with the stale token, whose
            callbacks the handler drops.
          * /clear is already safe: the runtime registers the session in
            _active_loops before this runs, and clear_conversation() refuses
            to wipe an active loop (FIX-CLEAR-ASK-RACE).
        """
        with self._prep_lock(session_key):
            # RACE-FIX v4 discipline: a newer send rotated the token.
            if self._turn_tokens.get(session_key) is not turn_token:
                logger.info(
                    "_prepare_turn_conversation: turn superseded for %s; "
                    "skipping preparation",
                    session_key,
                )
                return

            # SPEC-09 SP2: a WRITER holding a LIVE lease runs this turn in
            # its per-writer worktree (<project>/.worktrees/<id>, realpath'd
            # — SP0 BUG#14); everyone else (and any worktree failure) runs
            # in the project root. MED-6 reset: the lease-loss branch lives
            # in _worktree_for_turn via is_worktree_of on the CURRENT conv
            # path — an expired lease repoints the cwd at the project while
            # the worktree/branch survive for review.
            worktree_cwd = self._worktree_for_turn(
                session_key, agent_def, project_path
            )
            effective_project_path = worktree_cwd or project_path

            if rt.get_conversation(session_key) is None:
                loaded = rt.load_conversation(session_key)
                if loaded:
                    logger.info("send_to_special_agent: loaded persisted conversation for %s", session_key)
                    # Re-apply the active project to the loaded conversation. The
                    # persisted project_path and system_prompt may be stale (from a
                    # previous project the user had open). This is a no-op when
                    # the persisted values already match the active project.
                    rt._rebuild_conversation_context(
                        session_key,
                        effective_project_path,
                        agent_role=agent_def.role,
                    )

            if rt.get_conversation(session_key) is None:
                rt.create_conversation(
                    agent_name=agent_def.display_name,
                    session_key=session_key,
                    project_path=effective_project_path,
                    model=agent_model,               # Per-agent provider/model override
                    allowed_tools=agent_def.tools,   # Phase A: filtered tool set per agent
                    mcp_servers=agent_def.mcp_servers, # Phase B: MCP servers
                    agent_role=agent_def.role,        # §7: explicit role from definition
                    si_enforcement=si_enforcement,     # Per-agent enforcement gating
                    api_key=agent_def.api_key,        # Per-agent API key override
                    app_title=agent_def.app_title,  # OpenRouter X-Title header
                    fallback_provider=agent_def.fallback_provider,
                    # fallback_model removed in 2026-06-15 — runtime derives from provider card.
                    # See SPEC-AGENT-FALLBACK-MODEL-DROPDOWN-REMOVAL.md.
                    defer_prompt_build=True,        # NEW — prompt built in background thread
                )
            else:
                # Bug fix: sync existing conversation with latest agent definition.
                # When agent is edited (e.g. api_key added), the in-memory Conversation
                # retains stale values. Update api_key/model/app_title so edits take effect
                # immediately without requiring an app restart.
                conv = rt.get_conversation(session_key)
                if conv is not None:
                    # SPEC-09 SP2: keep the conversation's cwd in step with
                    # the lease state. A just-claimed lease repoints a hot
                    # conversation INTO the worktree (the write cwd lands
                    # without a restart); a lapsed lease repoints a stale
                    # worktree cwd back at the project root (LOW-8: the
                    # worktree/branch survive for review — never deleted
                    # here).
                    if worktree_cwd is not None:
                        if conv.project_path != worktree_cwd:
                            conv.project_path = worktree_cwd
                    elif (
                        conv.project_path
                        and project_path
                        and conv.project_path != project_path
                    ):
                        from utils.worktree_manager import is_worktree_of

                        if is_worktree_of(project_path, conv.project_path):
                            logger.info(
                                "turn for %s: lease gone — resetting stale "
                                "worktree cwd to project root",
                                session_key,
                            )
                            conv.project_path = project_path
                    if agent_def.api_key:
                        conv.api_key = agent_def.api_key
                    if agent_model:
                        conv.model = agent_model
                    if agent_def.app_title:
                        conv.app_title = agent_def.app_title
                    # Sync fallback config (in case agent was edited)
                    conv.fallback_provider = agent_def.fallback_provider
                    # Sync role (in case agent's role was edited)
                    if agent_def.role:
                        conv.agent_role = agent_def.role
                    # Sync MCP servers (in case agent's mcp_server list was edited)
                    if agent_def.mcp_servers is not None:
                        conv.mcp_servers = list(agent_def.mcp_servers)
                    # Sync SI enforcement (in case agent's self_improvement was edited)
                    if si_enforcement is not None:
                        conv.si_enforcement = si_enforcement
                    # conv.fallback_model assignment removed in 2026-06-15 — runtime derives from provider card.

            # Reset step_count on each new user message so the agent gets a
            # fresh step_limit budget per task. step_count counts assistant turns
            # (conversation.py:190), and without this reset it accumulates across
            # all tasks until hitting step_limit=100 and killing the agent.
            conv = rt.get_conversation(session_key)
            if conv is not None:
                conv.step_count = 0

            # SPEC-10 SP2b (D2 REV 2): snapshot (project_name, project_path,
            # write_cwd) for this turn — the completion-side checkpoint reads
            # THIS, never _active_project (project-tab switch mid-turn must
            # not mis-attribute, GAP-6). Keyed by session; token freshness is
            # enforced at completion (the prep's own token check already ran).
            # write_cwd is conv.project_path AFTER the lease/worktree
            # reconciliation above (worktree for leased writers, else root).
            if conv is not None and conv.project_path:
                self._turn_attr[session_key] = (
                    project_name or "(none)", project_path, conv.project_path,
                )
            else:
                self._turn_attr.pop(session_key, None)



    def reload_agents_and_mcp(
        self,
        *,
        on_complete: Callable[[], None] | None = None,
    ) -> None:
        """
        Reload agent registry and hot-reload MCP connections.

        Flow:
          1. reload_registry() — re-read YAML files from agents/
          2. Collect current agent prefixes BEFORE clearing self._agents
          3. Re-register all agents from the fresh registry
          4. disconnect_all() for all known prefixes — kill stale MCP subprocesses
          5. connect_servers() per agent — pre-warm MCP connections
          6. Call on_complete callback if provided

        Thread-safe: MCP operations are blocking; call from a background thread
        or via GLib.idle_add() if calling from a non-main thread that needs
        to update UI after completion.
        """
        from agent.special_agents import reload_registry, get_special_agents
        from utils.mcp_client import disconnect_all, connect_servers

        # 1. Reload registry from disk
        reload_registry()

        # 2. Collect current agent prefixes BEFORE clearing
        old_prefixes = list(self._agents.keys())

        # 3. Re-register all agents from fresh registry
        self._agents.clear()
        new_agents = get_special_agents()
        for agent_def in new_agents:
            self._agents[agent_def.conv_id_prefix] = agent_def

        # 4. Disconnect stale MCP connections for all known prefixes
        prefixes_to_disconnect = set(old_prefixes) | {a.conv_id_prefix for a in new_agents}
        for prefix in prefixes_to_disconnect:
            try:
                disconnect_all(conversation_key=prefix)
            except Exception as e:
                logger.warning(
                    "MCP disconnect failed for prefix %s: %s", prefix, e
                )

        # 5. Re-establish MCP connections for each agent
        for agent_def in new_agents:
            if agent_def.mcp_servers:
                try:
                    result = connect_servers(
                        server_names=agent_def.mcp_servers,
                        conversation_key=agent_def.conv_id_prefix,
                    )
                    for server_name, error in result.items():
                        if error:
                            logger.warning(
                                "MCP hot-reload: failed to connect %s for %s: %s",
                                server_name, agent_def.conv_id_prefix, error,
                            )
                except Exception as e:
                    logger.warning(
                        "MCP hot-reload: connection attempt failed for %s: %s",
                        agent_def.conv_id_prefix, e,
                    )

        logger.info("Agent registry and MCP connections reloaded")

        if on_complete:
            on_complete()

    # ── Chat box resolution ────────────────────────────────────────────────

    def _resolve_chat_box(self, session_key: str):
        """SPEC-12 R7: resolve the box from the turn reply key (slot first),
        else the direct tab, else the agent's project, else the ACTIVE
        project, else None (no project open, no tab)."""
        key = self._turn_reply_target.get(session_key) or session_key
        direct = self._mc.get_chat_box_for_session(key)
        if direct is not None:
            return direct
        project_name = None
        if self._agent_to_project is not None:
            project_name = self._agent_to_project.get_project(session_key)
        if project_name is None and self._active_project is not None:
            project_name = self._active_project[0]
        if project_name is not None:
            return self._mc.get_chat_box_for_session(f"project:{project_name}")
        return None

    def _reply_key(self, session_key: str) -> str:
        """SPEC-12 (R5 REV 3): the CURRENT turn's reply/display key — the
        per-send private target when set, else the routing (project) key.
        NEVER None (falls back to the session key), so a mount_key passed to
        render_sync/render_async is always a real key."""
        return (self._turn_reply_target.get(session_key)
                or self._resolve_mount_key(session_key)
                or session_key)

    def _resolve_mount_key(self, session_key: str) -> str:
        """SPEC-12 R7: the agent's project, else the ACTIVE open project,
        else the raw session key (no project open). NEVER None — a caller
        always gets a real key. Turn-scoped private targets are applied by
        the CALLER via _reply_key (R5 REV 3)."""
        project_name = None
        if self._agent_to_project is not None:
            project_name = self._agent_to_project.get_project(session_key)
        if project_name is None and self._active_project is not None:
            project_name = self._active_project[0]
        if project_name is not None:
            return f"project:{project_name}"
        return session_key

    # ── SPEC-08 SP4A: store-migration banner card + progress ─────────────────

    def _on_store_migration_complete(self, stats: dict) -> None:
        """SP4A banner receiver: build the FeedCardData from sweep stats.

        Runs in the runtime's dispatch context — on the main loop when GLib
        is wired (idle_add), inline in tests. Three card shapes (spec Edit 2):

        - success: migrated > 0 — "Transcript migration complete"
        - aborted: stats['aborted'] — "Transcript migration failed" (JSON
          untouched; the sweep retries next launch)
        - all-errors: migrated == 0, errors non-empty — "Transcript
          migration: N sessions need retry"

        Fires once per completed sweep: run_store_migration_once's
        process latch bounds the completions (the aborted-retry path may
        legitimately produce a second card when an in-process retry
        sweep runs — that card is informative, not a duplicate).
        """
        migrated = int(stats.get("migrated", 0) or 0)
        turns = int(stats.get("turns", 0) or 0)
        skipped = int(stats.get("skipped", 0) or 0)
        seconds = float(stats.get("seconds", 0.0) or 0.0)
        errors = stats.get("errors") or []
        kept = stats.get("kept_on_json") or []
        aborted = bool(stats.get("aborted", False))

        if aborted:
            title = "Transcript migration failed"
        elif migrated > 0:
            title = "Transcript migration complete"
        elif errors:
            title = f"Transcript migration: {len(errors)} sessions need retry"
        else:
            # Unreachable through the spec gate (migrated>0 / aborted /
            # errors); defensive no-card rather than an empty-stat card.
            return

        lines = [
            f"Migrated: {migrated} session(s), {turns} turn(s) in {seconds:.1f}s",
            f"Already current: {skipped}",
            f"Kept on JSON (diverged): {len(kept)}",
        ]
        if kept:
            shown = ", ".join(kept[:5]) + ("…" if len(kept) > 5 else "")
            lines.append(f"  (diverged: {shown})")
        lines.append(f"Errors: {len(errors)}")
        for sk, err in errors[:5]:
            lines.append(f"  {sk}: {err}")
        if len(errors) > 5:
            lines.append(f"  … and {len(errors) - 5} more")
        if aborted:
            lines.append(
                "JSON files untouched — migration will retry on next launch."
            )

        project_name = self._active_project[0] if self._active_project else "(none)"
        from models.feed_card import FeedCardData
        card = FeedCardData(
            card_type="system",
            source="system",
            title=title,
            body="\n".join(lines),
            author="Runtime",
            timestamp=datetime.now(timezone.utc),  # noqa: UP017 — module idiom (:545)
            project_name=project_name,
            metadata={"kind": "store_migration", "stats": stats},
        )
        if self._fh is None:
            logger.warning(
                "store-migration card skipped: no feed handler wired (headless)"
            )
            return
        try:
            self._fh.add_card(card)
        except Exception:
            logger.exception("store-migration card emission failed (non-fatal)")

    def _on_store_migration_progress(self, done: int, total: int) -> None:
        """SP4A progress heartbeat (≤ every 10 sessions). Logger-only: the
        drawer/UI surface for progress is post-MVP; the card reports the
        total sweep once. Logs at info — cheap, greppable, never spammy at
        the ≤-every-10 cadence."""
        logger.info(
            "[store-migration] progress: %d/%d sessions", done, total
        )

    # ── AgentRuntime callbacks (dispatched to render pipeline) ───────────────

    def _on_turn_start(self, session_key: str, _turn_token: object = None) -> None:
        """AgentRuntime turn-start callback (BUG #21 redesign).

        Dispatched once by the runtime at the top of _run_loop, BEFORE any
        LLM call or tool processing — for every turn, including tool-only
        turns. Runs in the runtime's dispatch context (GLib.idle_add wraps
        it onto the main thread in production). Delegates to _do_turn_start.
        """
        if self._GLib is not None:
            self._GLib.idle_add(self._do_turn_start, session_key, _turn_token)
        else:
            self._do_turn_start(session_key, _turn_token)

    def _do_turn_start(self, session_key: str, turn_start_token: object = None) -> None:
        """Main-thread portion of _on_turn_start (BUG #21 redesign).

        Starts the streaming bubble + fires the agent-start lifecycle for
        EVERY turn — including tool-only turns (which stream zero text
        deltas). The old mechanism (an empty text delta) never reached this
        logic: _do_text_delta_inner's empty-return fired first.

        Flag lifecycle (RACE-FIX v4, unchanged): _ended_sessions is cleared
        ONLY by send_to_special_agent at new-turn send time. This method
        does NOT clear it — clearing here would re-open the stale-delta
        race (a stale dispatch arriving after completion would clear the
        flag and start an orphan bubble). The guards below handle the two
        out-of-order cases:

        1. Stale cross-turn signal: turn_start_token doesn't match the
           current token (a newer turn's send already reassigned it) → drop.
        2. Terminal-first race: a terminal event for THIS turn (error /
           complete / cancel, same token) already landed on the main thread
           before this dispatch ran → session is in _ended_sessions → drop
           (starting a bubble now would orphan it after the error bubble).
        """
        if turn_start_token is not None:
            current_token = self._turn_tokens.get(session_key)
            if turn_start_token is not current_token:
                logger.debug(
                    "_do_turn_start: dropping stale turn-start (token mismatch) for %s",
                    session_key,
                )
                return
        if session_key in self._ended_sessions:
            logger.debug(
                "_do_turn_start: dropping turn-start for ended session %s "
                "(terminal event landed first)",
                session_key,
            )
            return
        if self._crh is None:
            return
        if not self._crh.is_streaming(session_key):
            chat_box = self._resolve_chat_box(session_key)
            if chat_box is not None:
                self._crh.start_streaming(session_key, chat_box, "Agent")
                # Fire lifecycle: agent started → ActivityHandler progress bar
                if self._on_agent_start_cb:
                    self._on_agent_start_cb(session_key)
                # Do NOT clear _ended_sessions here — send_to_special_agent
                # owns the clear (RACE-FIX v4; see docstring).
                # drawer-lifecycle start → drawer separator
                if self._on_drawer_lifecycle is not None:
                    agent_def_dl = self._agents.get(session_key)
                    agent_name_dl = agent_def_dl.display_name if agent_def_dl else "Agent"
                    self._on_drawer_lifecycle(session_key, agent_name_dl, "start")




    def _on_tool_call_start(
        self, session_key: str, name: str, args: dict[str, Any]
    ) -> None:
        """
        AgentRuntime tool call start callback.

        Phase D: Create an agent_action feed card so the PM sees tool activity.
        """
        if self._GLib is not None:
            self._GLib.idle_add(self._do_tool_call_start, session_key, name, args)
        else:
            self._do_tool_call_start(session_key, name, args)


    def _on_tool_call_result(
        self, session_key: str, name: str, result: Any, success: bool = True
    ) -> None:
        """
        AgentRuntime tool call result callback.

        Phase D: Update the agent_action feed card with the result.
        Phase 1.5 review staging: If write_file succeeds and review mode is active,
        stage the file to the shadow staging directory.

        Args:
            session_key: The agent's session key.
            name: Tool name (e.g. "read_file").
            result: Tool result text (string from ToolCall.mark_completed/mark_failed).
            success: Whether the tool succeeded. Default True for backward compat
                     with any other callers that don't pass it. The runtime now
                     dispatches this from the three dispatch sites in _run_loop.
        """
        if self._GLib is not None:
            self._GLib.idle_add(self._do_tool_call_result, session_key, name, result, success)
        else:
            self._do_tool_call_result(session_key, name, result, success)


    def _on_tool_call_approval_needed(
        self,
        session_key: str,
        tool_name: str,
        args: dict[str, Any],
    ) -> None:
        """
        AgentRuntime approval-needed callback.

        Phase E: Create a pending-approval feed card so the PM can Approve/Deny.
        """
        if self._GLib is not None:
            self._GLib.idle_add(self._do_approval_needed, session_key, tool_name, args)
        else:
            self._do_approval_needed(session_key, tool_name, args)

    def request_live_bridge_approval(self, method: str, params: dict,
                                     call_id: str) -> None:
        """SPEC-19 SP4 Part C: raise the EXISTING exec-approval card for a
        consequential live-bridge call (windowed pages can never approve
        themselves — the card is the only path).

        Same card shape + the same ``_pending_approvals`` registry as
        ``_do_approval_needed``; the resolution path is the PM clicking
        Approve/Deny on the card → ``approve_exec(card_id, ok)`` → the window
        observes the card resolution and forwards it to the bridge via
        ``set_live_bridge_resolver``. Method/params travel in ``args`` so
        ``approve_exec``'s existing card bookkeeping works unchanged; the
        runtime forward is a NO-OP for bridge calls (there is no in-flight
        runtime tool call) — the window's card-resolution hook is what
        resolves the bridge Promise.
        """
        if self._active_project is None or self._fh is None:
            logger.info(
                "live-bridge approval requested with no project/feed for %s",
                call_id)
            return
        project_name, _ = self._active_project
        preview = ", ".join(
            f"{k}={str(v)[:40]}" for k, v in list(params.items())[:3])
        card = FeedCardData(
            card_type="agent_action",
            source="agent",
            title=f"🖱️ Live page requests approval: {method}",
            body=f"{method}({preview}) — call {call_id}",
            author="Live section",
            timestamp=datetime.now(timezone.utc),
            project_name=project_name,
            metadata={
                "tool_name": method,
                "tool_args": params,
                "session_key": "special:live-bridge",
                "status": "pending_approval",
                "needs_approval": True,
                "live_bridge_call_id": call_id,
            },
        )
        card_id = self._fh.add_card(card)
        self._pending_approvals[card_id] = {
            "session_key": "special:live-bridge",
            "tool_name": method,
            "args": params,
            "live_bridge_call_id": call_id,
        }

    def _do_approval_needed(self, session_key: str, tool_name: str, args: dict) -> None:
        """Main-thread portion of _on_tool_call_approval_needed.

        Phase E: Create a pending-approval feed card. Store the approval info
        so approve_exec() can resolve it when the PM clicks Approve/Deny.
        """
        if self._active_project is None:
            # No active project — special agents require a project
            logger.info("Approval requested but no active project for %s", session_key)
            return

        if self._fh is None:
            logger.warning("_do_approval_needed: no feed handler available")
            return

        # V2 Silent bypass: if exec auto-accept is in silent mode, approve
        # directly without creating a feed card. The card is NOT stored
        # in _cards or _pending_approvals (no double-action possible).
        # Per SPEC-AUTO-ACCEPT-GRANULAR-1.md §2.5 BUG #11 fix: Silent mode
        # bypasses card creation entirely (no Approve/Deny buttons visible
        # on an already-executed command). Show mode still creates the
        # card for audit-trail purposes (Phase 7).
        if (self._on_check_exec_auto_accept is not None
                and self._on_check_exec_auto_accept() == "silent"):
            agent_def = self._agents.get(session_key)
            if agent_def is None:
                return
            runtime = self._runtimes.get(agent_def.runtime_id)
            if runtime is None:
                return
            # IMPORTANT: lambda captures session_key/tool_name/args by
            # closure. These are _do_approval_needed parameters (not loop
            # variables), so capture-by-closure is safe.
            # Note: spec A3 doesn't guard `self._GLib is not None`, but
            # every other call site in this class does (see _do_tool_call_start,
            # _do_text_delta, _maybe_prompt_project_trust). We follow the
            # same defensive pattern: in test mode without GTK, _GLib is None
            # and we call approve_exec directly (no main-thread dispatch needed).
            if self._GLib is not None:
                self._GLib.idle_add(
                    lambda: runtime.approve_exec(session_key, tool_name, args, True)
                )
            else:
                runtime.approve_exec(session_key, tool_name, args, True)
            return

        agent_def = self._agents.get(session_key)
        agent_name = agent_def.display_name if agent_def else "Agent"
        project_name, _ = self._active_project
        command = args.get("command", "unknown")

        card = FeedCardData(
            card_type="agent_action",
            source="agent",
            title=f"⚠️ {agent_name} requests approval to run command",
            body=f"$ {command}",
            author=agent_name,
            timestamp=datetime.now(timezone.utc),
            project_name=project_name,
            metadata={
                "tool_name": tool_name,
                "tool_args": args,
                "session_key": session_key,
                "status": "pending_approval",
                "needs_approval": True,
            },
        )
        card_id = self._fh.add_card(card)

        # Store approval info so approve_exec() can resolve it
        self._pending_approvals[card_id] = {
            "session_key": session_key,
            "tool_name": tool_name,
            "args": args,
        }

    def _on_response_complete(self, session_key: str, text: str, _turn_token: object = None) -> None:
        """
        AgentRuntime response complete callback.
        → End streaming and render the final text bubble.
        """
        if self._GLib is not None:
            self._GLib.idle_add(self._do_response_complete, session_key, text, _turn_token)
        else:
            self._do_response_complete(session_key, text, _turn_token)

    def _maybe_agent_checkpoint(self, session_key: str, turn_token: object) -> None:
        """SPEC-10 SP2b (D2 REV 2): COMPLETED-turn checkpoint for writer
        agents under an active review session.

        Gate order: writer → snapshot exists → token freshness → review gate
        (D2b: review_mode == "review" AND is_active()) → stop-all gate 1
        (D8b REV 2). The checkpoint itself runs on a background daemon
        thread; gate 2 re-checks stop-all just before the commit. Failure is
        non-fatal (D8c: log + skip, never an error card into the agent's
        chat). Locking per D3 REV 4b: a WORKTREE write cwd is agent-private —
        NO project lock; a PROJECT-ROOT write cwd takes the project accept
        lock (PM/accept paths mutate the root concurrently)."""
        agent_def = self._agents.get(session_key)
        if not getattr(agent_def, "can_write", False):
            return
        current_attr = self._turn_attr.get(session_key)
        if current_attr is None:
            return
        # Token freshness: the completion must carry the token that was
        # current at dispatch — a rotated token means a superseded turn.
        if self._turn_tokens.get(session_key) is not turn_token:
            return
        project_name, project_path, write_cwd = current_attr
        rh = self._review_handler
        if rh is None:
            return
        state = rh.get_state(project_name)
        if state is None or not (state.review_mode == "review"
                                 and state.is_active()):
            return
        # D8b REV 2 gate 1 (pre-flight, before the thread). NOTE: ARH owns
        # the stop-all registry — this is self.stop_all_in_progress(), NOT
        # a ReviewHandler reference (the SP2b brief's warning block).
        if self.stop_all_in_progress():
            self.note_stop_all_aborted()
            logger.info("agent checkpoint aborted: stop-all (gate 1) for %s",
                        session_key)
            return

        from utils.worktree_manager import is_worktree_of

        in_worktree = is_worktree_of(project_path, write_cwd)

        def _do():
            try:
                # SPEC-10 D8d: per-session checkpoint serialization. Two
                # same-session turns can overlap (turn N's checkpoint daemon
                # still running when N+1 completes); both write ONE tree.
                # Held across the WHOLE checkpoint body (init → stage →
                # gates → commit → enqueue); no eviction (session keys are
                # roster-bounded). Nesting order: checkpoint lock → project
                # lock (the root branch takes the project lock INSIDE it;
                # never the reverse).
                with self._checkpoint_locks.setdefault(
                        session_key, threading.Lock()):
                    # Worktree = agent-private tree: NO project lock (D3 REV 4b).
                    # Project root = shared with the PM/accept paths: project lock.
                    root_ctx = (self._project_lock_for(project_name)
                                if not in_worktree else _null_lock())
                    with root_ctx:
                        if not git_ops.is_repo(write_cwd):
                            init_result = git_ops.init_repo(write_cwd)
                            if not init_result.success:
                                logger.warning(
                                    "agent checkpoint: repo init failed for %s in %s: %s",
                                    session_key, write_cwd, init_result.error)
                                return
                        git_ops.stage_all(write_cwd)
                        # D8b REV 2 gate 2: stop-all may land while staging ran.
                        if self.stop_all_in_progress():
                            self.note_stop_all_aborted()
                            logger.info(
                                "agent checkpoint aborted: stop-all (gate 2) for %s",
                                session_key)
                            return
                        commit = git_ops.commit(
                            write_cwd, "[review] agent checkpoint",
                            allow_empty=True,          # D8c: SHA marker / sweep
                            agent_trailer=session_key,  # D1/SP1 fail-closed guard
                        )
                        if not commit.success:
                            # D8c: non-fatal — log + skip, never an error card.
                            logger.warning(
                                "agent checkpoint commit failed for %s in %s: %s",
                                session_key, write_cwd, commit.error)
                            return
                        rh.enqueue_agent_checkpoint(
                            project_name=project_name,
                            agent_key=session_key,
                            sha=commit.sha,
                            path_used=write_cwd,
                        )
            except Exception:
                # D8c: checkpoint failure never propagates into the turn.
                logger.exception(
                    "agent checkpoint thread failed for %s in %s",
                    session_key, write_cwd)

        threading.Thread(target=_do, daemon=True).start()

    def _project_lock_for(self, project_name: str):
        """The per-project accept lock, borrowed from the wired
        ReviewHandler (D3 REV 4b discipline: ONE lock family per project).
        Returns a no-op context when no ReviewHandler is wired (defensive —
        the gate above already returned in that case)."""
        rh = self._review_handler
        if rh is not None and hasattr(rh, "_project_lock_for"):
            return rh._project_lock_for(project_name)
        return _null_lock()


    def _on_token_usage(self, session_key: str, total_tokens: int, cost: float) -> None:
        """AgentRuntime token usage callback. Store and log."""
        self._session_usage[session_key] = (total_tokens, cost)
        logger.info(
            "Special agent token usage for %s: %d tokens, $%.4f",
            session_key,
            total_tokens,
            cost,
        )

    def get_session_usage(self) -> dict[str, tuple[int, float]]:
        """Return the in-memory session usage cache.

        Keyed by session_key. Values are (total_tokens, total_cost).
        Used by /cost command as fallback for agents without conversation files.
        Returns a defensive copy.
        """
        return dict(self._session_usage)

    def _on_token_breakdown(self, session_key: str, breakdown: dict) -> None:
        """§Phase-A — Per-turn token budget breakdown. Store + log + dispatch.

        Breakdown dict keys (verified at models/conversation.py:362 and
        runtime.py:2187-2218):
          system_prompt_tokens  (int)
          conversation_tokens   (int)
          total_used_tokens     (int)
          model_max_tokens      (int)
          remaining_tokens      (int)
          usage_percent         (float 0.0-100.0)
          trimmed_this_turn     (bool)  ← only True when real compaction happened
          messages_remaining    (int)
          messages_removed_this_turn (int, 0 if no compaction)
          compaction_event      (dict, only when _compaction_happened)
        """
        try:
            # Always log for observability (preserve existing behavior).
            logger.info(
                "[token-breakdown] sk=%s system_prompt=%d conv=%d total=%d/%d "
                "remaining=%d (%.1f%%) trimmed=%s removed=%d",
                session_key,
                breakdown["system_prompt_tokens"],
                breakdown["conversation_tokens"],
                breakdown["total_used_tokens"],
                breakdown["model_max_tokens"],
                breakdown["remaining_tokens"],
                breakdown["usage_percent"],
                breakdown.get("trimmed_this_turn", False),
                breakdown.get("messages_removed_this_turn", 0),
            )

            # Cache for the UI meter (cheap, in-memory).
            self._last_breakdown[session_key] = breakdown

            # Fire compaction bubble on real compaction only.
            if breakdown.get("trimmed_this_turn", False):
                already_seen = self._first_compaction_seen.get(session_key, False)
                if not already_seen:
                    self._first_compaction_seen[session_key] = True
                    ev = breakdown.get("compaction_event", {})
                    if self._GLib is not None:
                        self._GLib.idle_add(
                            self._do_compaction_bubble, session_key, ev
                        )
                    else:
                        self._do_compaction_bubble(session_key, ev)

            # Threshold warnings — 80% → "approaching limit", 95% → "auto-compact imminent".
            # Anti-spam: hysteresis — only re-fire if we cross back below 75%.
            usage_pct = breakdown.get("usage_percent", 0.0)
            last_pct = self._last_warning_pct.get(session_key, -1.0)
            new_warn_level: str | None = None
            if usage_pct >= 95.0 and last_pct < 95.0:
                new_warn_level = "auto-compact-imminent"
            elif usage_pct >= 80.0 and last_pct < 80.0:
                new_warn_level = "approaching-limit"
            if new_warn_level is not None:
                self._last_warning_pct[session_key] = usage_pct
                if self._GLib is not None:
                    self._GLib.idle_add(
                        self._do_usage_warning, session_key, new_warn_level, usage_pct
                    )
                else:
                    self._do_usage_warning(session_key, new_warn_level, usage_pct)
            # Reset hysteresis when we drop back well below threshold.
            if usage_pct < 75.0 and last_pct >= 80.0:
                self._last_warning_pct[session_key] = usage_pct

            # Phase A — Forward to optional extra listener (context meter).
            if self._on_token_breakdown_extra is not None:
                try:
                    self._on_token_breakdown_extra(session_key, breakdown)
                except Exception:
                    logger.exception(
                        "_on_token_breakdown: extra listener raised; ignoring"
                    )
        except Exception:
            logger.exception("_on_token_breakdown: failed for %s", session_key)

    def _do_compaction_bubble(self, session_key: str, ev: dict) -> None:
        """Main-thread portion of the compaction bubble dispatch.

        Renders a styled bubble into the chat box for the session.
        Mirrors _do_error's pattern (line 1286): resolve chat_box, call
        self._crh.render_sync, chat_box.append(bubble), scroll to bottom.
        """
        logger.debug("[handler] _do_compaction_bubble: sk=%s ev=%s", session_key, ev)
        if self._crh is not None:
            # BUG #22 guard: tool-only turn — no empty bubble on cleanup.
            streaming_text = self._crh.get_streaming_text(session_key) or ""
            self._crh.end_streaming(
                session_key, agent_name=None, render=bool(streaming_text.strip()),
                # FIX 4 (SP5a r3): thread the resolved mount key like the
                # main :2102 path — the final row mounts in the project box.
                mount_key=self._reply_key(session_key),
            )

        chat_box = self._resolve_chat_box(session_key)
        if chat_box is None:
            logger.debug("[handler] _do_compaction_bubble: no chat box for %s", session_key)
            return

        removed = int(ev.get("messages_removed", 0))
        freed = int(ev.get("tokens_freed", 0))
        layer = int(ev.get("layer", 0))
        trigger = str(ev.get("trigger", ""))
        text = (
            f"🧹 Context reset. Removed {removed} message"
            f"{'s' if removed != 1 else ''}, freed ~{freed:,} tokens."
            f"\n   (Layer {layer}; trigger: {trigger})"
        )
        bubble = self._crh.render_sync(
            "Agent", text, session_key, agent_name=None,
            mount_key=self._reply_key(session_key),
        )
        if bubble is not None:
            chat_box.append(bubble)
        else:
            logger.warning("[handler] _do_compaction_bubble: render_sync returned None")

    def _do_usage_warning(
        self, session_key: str, level: str, usage_pct: float
    ) -> None:
        """Main-thread portion of context-pressure warning.

        Levels:
          "approaching-limit" — usage >= 80%, suggest /compact.
          "auto-compact-imminent" — usage >= 95%, expect auto-compaction.
        """
        if level == "approaching-limit":
            text = (
                f"⚠️ Context at {usage_pct:.0f}%. "
                f"Consider /compact to free space."
            )
        else:  # auto-compact-imminent
            text = (
                f"🔴 Context at {usage_pct:.0f}%. "
                f"Auto-compaction will trigger soon."
            )
        chat_box = self._resolve_chat_box(session_key)
        if chat_box is None:
            return
        if self._crh is not None:
            # BUG #22 guard: tool-only turn — no empty bubble on cleanup.
            streaming_text = self._crh.get_streaming_text(session_key) or ""
            self._crh.end_streaming(
                session_key, agent_name=None, render=bool(streaming_text.strip()),
                # FIX 4 (SP5a r3): resolved mount key threaded like :2102.
                mount_key=self._reply_key(session_key),
            )
            bubble = self._crh.render_sync(
                "Agent", text, session_key, agent_name=None,
                mount_key=self._reply_key(session_key),
            )
            if bubble is not None:
                chat_box.append(bubble)

    # ── Phase A: public API for the UI context meter ─────────────────────────
    def get_last_breakdown(self, session_key: str) -> dict | None:
        """Return the most recent token breakdown for ``session_key``.

        Returns None if the session hasn't seen a turn yet.
        Used by the chat-panel context meter to render a live progress bar.
        """
        return self._last_breakdown.get(session_key)

    def set_on_token_breakdown_extra(
        self, cb: Callable[[str, dict], None] | None
    ) -> None:
        """Inject an additional listener for breakdown events.

        Used by Phase A — the context meter subscribes here without
        replacing the existing logger.info dispatch. None clears.
        """
        self._on_token_breakdown_extra = cb

    def _on_error(self, session_key: str, message: str, _turn_token: object = None) -> None:
        """AgentRuntime error callback. Show error bubble."""
        if isinstance(message, BaseException):
            self._last_error_exception[session_key] = message
        else:
            self._last_error_exception[session_key] = None
        if self._GLib is not None:
            self._GLib.idle_add(self._do_error, session_key, message, _turn_token)
        else:
            self._do_error(session_key, message, _turn_token)

    def _on_enforcement_status(self, session_key: str, tool_name: str, status: dict) -> None:
        """§F — Enforcement status callback. Log enforcement results to observability log.

        The status dict format is defined by ENFORCEMENT_LAYER_SPEC.md §8.2:
            {
                "tier": "syntax" | "tests" | "lint",
                "file": "src/auth.py",
                "passed": True | False,
                "detail": "Syntax check passed for src/auth.py",
            }

        The UI layer decides how to render this (feed card text, icons, etc.).
        This callback just provides the data — rendering is handled separately.
        """
        icon = "✅" if status["passed"] else "❌"
        logger.info(
            "[enforcement:%s] sk=%s %s %s — %s",
            status["tier"],
            session_key,
            tool_name,
            icon,
            status["detail"],
        )

