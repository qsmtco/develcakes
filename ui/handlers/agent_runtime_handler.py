# ui/handlers/agent_runtime_handler.py
# Phase 1.4 — Wires AgentRuntime into CrabCakes UI as a special agent.
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
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from gi.repository import GLib

logger = logging.getLogger(__name__)


class AgentRuntimeHandler:
    """
    Wires AgentRuntime into the CrabCakes UI.

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

        # Shared routing table — set via set_agent_routing() (maps session_key → project_name)
        # Used to route special agent responses to project chat boxes when no direct tab exists.
        self._agent_to_project = None

        # Registered agents: session_key → SpecialAgentDef (full definition)
        self._agents: dict[str, Any] = {}
        # Active project: (name, path) or None
        self._active_project: tuple[str, str] | None = None
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

    def clear_conversation(self, session_key: str) -> bool:
        """Reset a special agent's conversation in place.

        Resets messages=[], step_count=0, total_tokens=0, total_cost=0.0,
        and invalidates the token-estimate cache. Also deletes the persisted
        conversation JSON so the next session start loads a fresh state.

        In-place reset (vs remove + recreate) avoids races with in-flight
        tool loops: a background thread may be reading conv.messages via
        the runtime's _run_loop; resetting the list is safer than
        deleting the conversation object and recreating it, because the
        object identity stays stable for the running thread.

        Returns True on success, False if the session isn't a registered
        special agent or has no runtime/conversation.

        Spec: docs/specs/STEP-COUNT-RESET-FIX.md Edit 4.
        """
        # Guard: only special-agent sessions can be cleared this way.
        # `special:coder`, `special:debugger`, `special:crabcakes`, etc.
        if not isinstance(session_key, str) or not session_key.startswith("special:"):
            logger.warning(
                "clear_conversation: refusing non-special session_key=%r",
                session_key,
            )
            return False

        agent_def = self._agents.get(session_key)
        if agent_def is None:
            logger.warning(
                "clear_conversation: no registered special agent for %s",
                session_key,
            )
            return False

        # Resolve the runtime that owns this session. Display name is the
        # key in self._runtimes; _get_runtime will lazily create one if
        # the agent has never been used yet (clear-before-first-use is a
        # legitimate no-op case).
        try:
            rt = self._get_runtime(agent_def.display_name, agent_def=agent_def)
        except Exception as exc:
            logger.error(
                "clear_conversation: failed to acquire runtime for %s: %s",
                session_key, exc,
            )
            return False

        # In-place reset. Keep the Conversation object identity so any
        # in-flight _run_loop thread continues to see the same object.
        # FIX-CLEAR-ASK-RACE: refuse to wipe a conversation that an in-flight
        # _run_loop is actively reading. The /clear + /ask pairing rule can
        # fire /clear while the /ask thread is between add_user_message and
        # to_api_messages; wiping conv.messages at that instant produces a
        # system-only payload that MiniMax rejects (status_code=2013). Refuse
        # instead; the user can retry /clear once the loop finishes.
        if rt.is_loop_active(session_key):
            logger.warning(
                "clear_conversation: refusing reset for %s — tool loop is active; retry after it completes",
                session_key,
            )
            return False

        conv = rt.get_conversation(session_key)
        if conv is not None:
            try:
                conv.messages = []
                conv.step_count = 0
                conv.total_tokens = 0
                conv.total_cost = 0.0
                # _token_estimate_cache is keyed on (len(messages), hash(system_prompt))
                # — messages are now empty, so the cache MUST be invalidated
                # or the next trim pass will read a stale value.
                conv._token_estimate_cache = None
            except Exception as exc:
                logger.error(
                    "clear_conversation: in-place reset failed for %s: %s",
                    session_key, exc,
                )
                return False
            logger.info(
                "clear_conversation: reset in-memory conversation for %s",
                session_key,
            )

        # Delete the persisted JSON so a restart doesn't restore the old
        # state. Best-effort: a missing file is fine (nothing to delete),
        # other OSErrors are logged but don't fail the whole operation —
        # the in-memory state is already cleared.
        try:
            from utils.config import get_config_dir
            import os
            conv_dir = os.path.join(get_config_dir(), "conversations")
            conv_path = os.path.join(conv_dir, f"{session_key}.json")
            os.remove(conv_path)
            logger.info(
                "clear_conversation: deleted persisted conversation %s",
                conv_path,
            )
        except FileNotFoundError:
            pass  # No persisted file — that's fine.
        except OSError as exc:
            logger.warning(
                "clear_conversation: could not delete persisted file for %s: %s",
                session_key, exc,
            )

        return True

    def compact_conversation(
        self, session_key: str, focus_text: str = ""
    ) -> dict:
        """Force compaction of a special agent's conversation.

        Spec: docs/specs/SPEC-CONTEXT-UI-COMPACT-LLM-2026-07-10.md §3.2.

        Args:
            session_key: "special:coder" etc.
            focus_text: Optional focus instructions for Phase C's LLM
                strategy (Phase B's textual strategy ignores this).

        Returns:
            dict with keys:
                messages_removed (int)
                tokens_freed (int)
                summary_chars (int)
                layer (int)

        Returns an empty dict {"messages_removed": 0, ...} on failure.
        """
        if not isinstance(session_key, str) or not session_key.startswith("special:"):
            logger.warning(
                "compact_conversation: refusing non-special session_key=%r",
                session_key,
            )
            return {"messages_removed": 0, "tokens_freed": 0, "summary_chars": 0, "layer": 0}

        agent_def = self._agents.get(session_key)
        if agent_def is None:
            logger.warning(
                "compact_conversation: no registered special agent for %s",
                session_key,
            )
            return {"messages_removed": 0, "tokens_freed": 0, "summary_chars": 0, "layer": 0}

        try:
            rt = self._get_runtime(agent_def.display_name, agent_def=agent_def)
        except Exception as exc:
            logger.error(
                "compact_conversation: failed to acquire runtime for %s: %s",
                session_key, exc,
            )
            return {"messages_removed": 0, "tokens_freed": 0, "summary_chars": 0, "layer": 0}

        conv = rt.get_conversation(session_key)
        if conv is None:
            return {"messages_removed": 0, "tokens_freed": 0, "summary_chars": 0, "layer": 0}

        try:
            _, hard_ceiling = rt._compute_compaction_threshold(conv)
        except Exception:
            logger.exception(
                "compact_conversation: failed to resolve hard_ceiling; using 128K"
            )
            hard_ceiling = 128_000

        target_budget = max(4_000, hard_ceiling // 2)
        messages_before = len(conv.messages)
        tokens_before = conv.get_token_estimate()

        strat_name = getattr(agent_def, "compaction_strategy", "textual")
        if strat_name == "llm" and hasattr(rt, "force_llm_compact"):
            # Phase C path — fires when force_llm_compact exists and
            # agent_def.compaction_strategy == "llm".
            try:
                return rt.force_llm_compact(conv, target_budget, focus_text, agent_def=agent_def)
            except Exception:
                logger.exception(
                    "compact_conversation: LLM strategy failed; "
                    "falling back to textual"
                )
                # Fall through to textual default.

        rt.force_compact(conv, target_budget)

        try:
            from agent.persistence import save_conversation_to_disk
            save_conversation_to_disk(conv, session_key)
        except Exception:
            logger.exception(
                "compact_conversation: persist failed; in-memory compact succeeded"
            )

        ev = rt._context_strategy.last_result
        tokens_after = conv.get_token_estimate()
        if ev is None:
            return {
                "messages_removed": 0,
                "tokens_freed": max(0, tokens_before - tokens_after),
                "summary_chars": 0,
                "layer": 0,
            }
        return {
            "messages_removed": ev.messages_removed,
            "tokens_freed": ev.tokens_freed,
            "summary_chars": ev.summary_tokens_injected,
            "layer": ev.layer,
        }

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

    # ── AgentRuntime lifecycle ────────────────────────────────────────────────

    def _resolve_agent_model(self, agent_def: Any) -> str | None:
        """Resolve the model string for an agent definition.

        Uses agent-specific llm_name to look up the provider in providers.yaml,
        then resolves the model from the provider's default_model.

        Returns:
            Full model string like "minimax/MiniMax-M2.7", or None to use
            the runtime's default_model.
        """
        llm_name = getattr(agent_def, "llm_name", None)

        if not llm_name:
            return None

        try:
            from agent.config import load_agent_config
            config = load_agent_config()
            prov_cfg = config.providers.get(llm_name)
            if prov_cfg and prov_cfg.default_model:
                if "/" in prov_cfg.default_model:
                    return prov_cfg.default_model
                return f"{llm_name}/{prov_cfg.default_model}"
        except Exception:
            logger.warning("Cannot resolve provider default model for %s", llm_name)
        return llm_name  # fallback — runtime will try to resolve

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
        rt.start()
        self._runtimes[name] = rt
        logger.info("Created AgentRuntime for special agent: %s", name)
        return rt

    @staticmethod
    def _parse_providers_file_strict(path: str) -> list[Any] | None:
        """Strictly parse providers.yaml, distinguishing valid-empty from corrupt.

        utils.providers_store.load_providers/_parse swallow every parse error
        (warning + []), so "missing file", "corrupt file" and "file is []" all
        look identical through the public API. BUG 2 (SPEC-01 Phase 1): the
        refresh guard must fire only for the first two — an existing file that
        parses to an empty list is an intentional removal (Settings deleted
        the last provider) and must apply.

        Mirrors _parse's deserialization chain: PyYAML when importable, json
        fallback otherwise. Returns the parsed list ([] for a valid empty
        document) or None when the content is unreadable or unparseable, so a
        hostile file can never abort a Settings save's refresh side effect.
        """
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError:
            return None  # unreadable — treated as missing by the caller

        try:
            import yaml

            raw: Any = yaml.safe_load(text)
        except ImportError:
            try:
                raw = json.loads(text)
            except (json.JSONDecodeError, ValueError):
                return None
        except yaml.YAMLError:
            # PyYAML parser errors (ScannerError/ParserError/…) — corrupt content.
            return None

        # Same shape contract as providers_store._parse: anything that is not
        # a list is malformed (its per-item skip-warnings stay in _parse).
        return raw if isinstance(raw, list) else None

    def refresh_provider_config(self) -> None:
        """Reload providers.yaml and update every cached runtime's config in place.

        Called when Settings saves a provider (on_providers_changed → wire_settings_handler
        → this). Conversations and runtimes are NOT recreated — only the provider dict
        is swapped so base_url/caller/max_tokens edits take effect on the next call
        without an app restart. (SPEC-01: stale-cache-divergence fix.)

        Semantics (SPEC-01 Phase 1):
          * Missing or unparseable providers.yaml → keep cached snapshots + warn.
            An EXISTING file that parses to an empty list is an intentional
            removal (Settings deleted the last provider) and applies.
          * Every runtime receives its own clone of the fresh provider values:
            Phase 2 resolves base_url/caller live by mutating the runtime's
            provider dict in place, so one shared object would leak one
            agent's resolution into every other agent.
          * One failing runtime logs and is skipped; the batch continues.
        """
        from agent.config import load_agent_config
        from utils.providers_store import get_providers_path

        # BUG 2: distinguish valid-empty from missing/corrupt. load_providers()
        # maps all three to [], so probe the file directly: it must exist AND
        # parse. Missing/unparseable keeps the old snapshot; existing-empty
        # falls through and applies.
        yaml_path = get_providers_path()
        file_is_loadable = os.path.isfile(yaml_path) and (
            self._parse_providers_file_strict(yaml_path) is not None
        )
        fresh = load_agent_config()
        if not fresh.providers and not file_is_loadable:
            logger.warning(
                "refresh_provider_config: providers.yaml is missing or corrupt "
                "— keeping existing runtime snapshots"
            )
            return
        # Iterate a snapshot: _get_runtime() may insert into _runtimes while
        # this loop runs — mutating a dict during iteration raises
        # RuntimeError. list() copies once; the loop body touches only the
        # runtime objects, never the dict itself. (Both callers run on the
        # main thread; the copy is defensive hardening, not a race guard.)
        updated = 0
        for name, rt in list(self._runtimes.items()):
            try:
                cfg = rt._config
                providers = cfg.providers
                # BUG 6: clone each fresh value into this runtime's own dict —
                # never share the fresh dict or its entries across runtimes
                # (live base_url/caller resolution mutates the dict in place,
                # and one shared object would leak one agent's resolution into
                # every other agent). dataclasses.replace() clones a
                # ProviderConfig-style dataclass; deepcopy covers any other
                # type. The clone is built BEFORE mutating `providers` so a
                # clone failure leaves the old snapshot intact.
                cloned = {
                    key: replace(value)
                    if is_dataclass(value) and not isinstance(value, type)
                    else copy.deepcopy(value)
                    for key, value in fresh.providers.items()
                }
                providers.clear()
                providers.update(cloned)
                updated += 1
            except Exception:
                logger.exception(
                    "refresh_provider_config: failed to update runtime %r — skipped",
                    name,
                )
        if updated:
            logger.info("refresh_provider_config: updated %d runtime(s)", updated)

    # ── Public: send a message to a special agent ────────────────────────────

    def send_to_special_agent(self, session_key: str, text: str) -> None:
        """
        Send a user message to a special agent for processing.

        Called by ChatHandler.on_send() when the target tab is a special agent.

        Requires an active project — special agents are project-scoped.
        """
        agent_def = self._agents.get(session_key)
        if agent_def is None:
            logger.warning(
                "send_to_special_agent: %s is not a registered special agent",
                session_key,
            )
            return

        # Special agents require an active project.
        # (The KB-helper carve-out died with the KB stack, SPEC-04.)
        if self._active_project is None:
            if self._GLib is not None:
                self._GLib.idle_add(self._do_error, session_key,
                                    "Open a project first. Special agents work within projects.")
            else:
                self._do_error(session_key,
                               "Open a project first. Special agents work within projects.")
            return

        if self._active_project is not None:
            project_name, project_path = self._active_project
        else:
            project_name, project_path = "(none)", None
        rt = self._get_runtime(agent_def.display_name, agent_def=agent_def)

        logger.debug("[handler] send_to_special_agent: sk=%s agent=%s project=%s text_len=%d",
                     session_key, agent_def.display_name, project_name, len(text))

        # Resolve per-agent model override (Step 4 — user-defined agents)
        agent_model = self._resolve_agent_model(agent_def)

        # Resolve per-agent SI enforcement flag
        si_enforcement = None
        if hasattr(agent_def, 'get_self_improvement_config'):
            si_cfg = agent_def.get_self_improvement_config()
            si_enforcement = si_cfg.get('enforcement')

        # Create conversation if it doesn't exist yet, with project context and filtered tools.
        # First try to load the persisted conversation from disk (preserves message history,
        # token/cost data, and other state across app restarts). Only create fresh if no
        # persisted conversation exists.
        #
        # Phase 4 Part B (SPEC-UI-RESPONSIVENESS-2 §2.4): this whole block —
        # plus the per-agent state sync and step-count reset below — now runs
        # on the RUNTIME LOOP thread via the `prepare` callback handed to
        # send_message(), not on the GTK main thread (see
        # _prepare_turn_conversation).
        #
        # RACE-FIX v4: Clear the ended/completed flags and assign a NEW turn
        # token for this session. This is the ONLY place these should be
        # cleared. The new token ensures stale deltas from the previous turn
        # (which captured the OLD token) are rejected by the token mismatch
        # check in _do_text_delta. It is assigned HERE, on the main thread,
        # BEFORE the loop thread starts — the loop captures it via
        # send_message() and _prepare_turn_conversation checks it.
        self._ended_sessions.discard(session_key)
        self._session_completed.discard(session_key)
        # RACE-FIX v4b: Assign a new turn token ON THE RUNTIME object.
        # _dispatch captures this token at call time (background thread, stable).
        # The handler's _on_* callbacks receive it as a kwarg — they don't
        # read from a mutable dict that could change between dispatch and execution.
        new_token = object()
        self._turn_tokens[session_key] = new_token
        rt._turn_token = new_token

        # Phase 4 Part B: the main thread keeps only the turn-token setup and
        # the thread start. Everything that used to block here (conversation
        # disk load + deserialize, project reconciliation, per-agent state
        # sync, step-count reset) runs on the loop thread.
        def _prepare_turn() -> None:
            self._prepare_turn_conversation(
                rt=rt,
                session_key=session_key,
                agent_def=agent_def,
                project_path=project_path,
                agent_model=agent_model,
                si_enforcement=si_enforcement,
                turn_token=new_token,
            )

        rt.send_message(session_key, text, prepare=_prepare_turn)

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

    def _prepare_turn_conversation(
        self,
        *,
        rt,
        session_key: str,
        agent_def: Any,
        project_path: str | None,
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
                        project_path,
                        agent_role=agent_def.role,
                    )

            if rt.get_conversation(session_key) is None:
                rt.create_conversation(
                    agent_name=agent_def.display_name,
                    session_key=session_key,
                    project_path=project_path,
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

    def stop_all(self) -> None:
        """Stop all agent runtimes. Called on window shutdown."""
        # BUG #31: Clean up MCP connections before stopping runtimes
        try:
            from utils.mcp_client import disconnect_all as mcp_disconnect_all
            mcp_disconnect_all()  # Clean up all MCP connections across all conversations
        except Exception:
            pass  # Best effort — daemon threads die on exit anyway

        for name, rt in list(self._runtimes.items()):
            rt.stop()
        self._runtimes.clear()


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
        """Resolve the chat box for a session key.

        If no direct tab exists (e.g. special agent messaged from project group chat),
        looks up the project via AgentRoutingTable and returns the project chat box.
        """
        chat_box = self._mc.get_chat_box_for_session(session_key)
        if chat_box is not None:
            return chat_box
        # No direct tab — check if this agent is routed to a project
        if self._agent_to_project is not None:
            project_name = self._agent_to_project.get_project(session_key)
            if project_name is not None:
                logger.debug("[handler] _resolve_chat_box: sk=%s → project:%s", session_key, project_name)
                return self._mc.get_chat_box_for_session(f"project:{project_name}")
        logger.debug("[handler] _resolve_chat_box: sk=%s → None (no tab, no routing)", session_key)
        return None

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

    def _on_text_delta(self, session_key: str, text: str, _turn_token: object = None) -> None:
        """
        AgentRuntime text delta callback.
        → Start or update a streaming bubble in the UI.

        AC3 Part A — producer-side coalescing. Runs in the runtime's dispatch
        context (in production the runtime's _dispatch already wraps this
        callback in GLib.idle_add, so this executes on the main thread; in
        test mode without GTK it runs on the caller's thread). Accumulates
        text immediately and schedules at most one main-thread render
        dispatch per _delta_throttle_sec, with an unconditional trailing
        re-schedule so the last batch is never dropped. The win is the
        idle_add-queue flood reduction: N deltas no longer enqueue N
        render dispatches.
        """
        # Empty deltas bypass coalescing: providers may send empty content
        # deltas (delta: {content: ""}); routing them uncoalesced is cheap
        # and _do_text_delta_inner's empty-return makes them a no-op when
        # no text has accumulated. (The turn-start signal used to ride this
        # path as an empty delta — it moved to the dedicated on_turn_start
        # callback; see _do_turn_start.)
        if not text:
            if self._GLib is not None:
                self._GLib.idle_add(self._do_text_delta, session_key, "", _turn_token)
            else:
                self._do_text_delta(session_key, "", _turn_token)
            return
        if self._crh is None:
            return  # no render pipeline — nothing to accumulate or schedule
        # Producer-side stale guards (moved with the accumulation): a delta
        # for an ended session or from a previous turn must neither corrupt
        # the accumulated text nor schedule work. _turn_token=None (legacy
        # 2-arg callers) is never stale. The ended flag is cleared by
        # send_to_special_agent when a NEW turn starts.
        if session_key in self._ended_sessions:
            logger.debug(
                "_on_text_delta: dropping delta for ended session %s", session_key,
            )
            return
        if _turn_token is not None:
            current = self._turn_tokens.get(session_key)
            if _turn_token is not current:
                logger.debug(
                    "_on_text_delta: dropping stale delta (token mismatch) for %s",
                    session_key,
                )
                return
        # Producer-side accumulation (was per-delta on the main thread — an
        # O(n) copy per delta flooding the main loop; audit finding #2).
        self._streaming_text[session_key] = self._streaming_text.get(session_key, "") + text
        now = time.monotonic()
        if (session_key not in self._delta_dispatch_pending
                and now - self._last_delta_dispatch.get(session_key, 0.0) >= self._delta_throttle_sec):
            # Schedule with the CURRENT turn token, not the delta's: the
            # dispatch may outlive several deltas and must carry the turn it
            # will render. _do_text_delta_inner still guards a mid-batch
            # mismatch the same way as before.
            self._delta_dispatch_pending.add(session_key)
            self._last_delta_dispatch[session_key] = now
            self._delta_dirty.discard(session_key)
            current_token = self._turn_tokens.get(session_key)
            if self._GLib is not None:
                self._GLib.idle_add(self._do_text_delta, session_key, "", current_token)
            else:
                self._do_text_delta(session_key, "", current_token)
        else:
            # Not scheduling: this delta's text is not covered by any queued
            # dispatch — mark dirty so the in-flight dispatch's finally
            # schedules an unconditional trailing dispatch for it.
            self._delta_dirty.add(session_key)

    def _do_text_delta(self, session_key: str, text: str = "", delta_token: object = None) -> None:
        """Main-thread portion of _on_text_delta (AC3 Part A: coalesced).

        Text is accumulated in the dispatch context (see _on_text_delta —
        in production both run on the main thread via the runtime's
        GLib.idle_add wrapper; the accumulation happens once per delta
        instead of once per render dispatch); this dispatch renders the
        accumulated text. The `text` parameter is retained for legacy
        direct callers and provider-sent empty deltas (empty `text` with no
        accumulated text returns before any rendering).

        _delta_dispatch_pending is cleared on EVERY return path (finally). If
        deltas arrived while this dispatch was in flight (dirty flag), exactly
        one UNCONDITIONAL trailing dispatch is re-scheduled — the last delta
        batch always produces a final update_streaming before completion reads
        the text.
        """
        try:
            self._do_text_delta_inner(session_key, text, delta_token)
        finally:
            self._delta_dispatch_pending.discard(session_key)
            if session_key in self._delta_dirty:
                self._delta_dirty.discard(session_key)
                self._delta_dispatch_pending.add(session_key)
                self._last_delta_dispatch[session_key] = time.monotonic()
                current_token = self._turn_tokens.get(session_key)
                if self._GLib is not None:
                    self._GLib.idle_add(self._do_text_delta, session_key, "", current_token)
                else:
                    self._do_text_delta(session_key, "", current_token)

    def _do_text_delta_inner(self, session_key: str, text: str, delta_token: object = None) -> None:
        """Render body of _do_text_delta (guards + bubble-start + render).

        The throttle lives entirely in the producers (the scheduler in
        _on_text_delta and the trailing scheduler in _do_text_delta's
        finally): every dispatch that reaches this method was already gated,
        so it renders unconditionally.
        """
        if self._crh is None:
            return
        # Skip empty text deltas — OpenRouter sends delta: {content: ""} with
        # finish_reason:"error". Starting a streaming bubble on empty text
        # creates a flickering empty box that is immediately replaced by
        # the error message.
        if not text and not self._streaming_text.get(session_key):
            return
        # RACE-FIX: If this session has already completed (response_complete
        # or error set _ended_sessions), drop the delta. This prevents stale
        # idle callbacks from starting a new streaming bubble AFTER the final
        # bubble has been rendered. The flag is cleared by send_to_special_agent
        # when a NEW turn starts — NOT here (that was the old bug).
        if session_key in self._ended_sessions:
            logger.debug(
                "_do_text_delta: dropping delta for ended session %s", session_key,
            )
            return
        # RACE-FIX v4: If this dispatch's turn token doesn't match the current
        # turn token, it's from a previous turn (stale). Drop it.
        # delta_token=None means a 2-arg caller (backward-compat tests) —
        # treat as current turn (never stale).
        # Unlike a generation counter, the token does NOT change at completion
        # time, so same-turn deltas are never wrongly dropped.
        if delta_token is not None:
            current_token = self._turn_tokens.get(session_key)
            if delta_token is not current_token:
                logger.debug(
                    "_do_text_delta: dropping stale delta (token mismatch) for %s",
                    session_key,
                )
                return
        if not self._crh.is_streaming(session_key):
            # Degradation path: starts the bubble if turn-start didn't (legacy
            # callers with on_turn_start=None, or a missed turn-start signal).
            chat_box = self._resolve_chat_box(session_key)
            if chat_box is not None:
                self._crh.start_streaming(session_key, chat_box, "Agent")
                # Fire lifecycle: agent started → ActivityHandler progress bar
                if self._on_agent_start_cb:
                    self._on_agent_start_cb(session_key)
                # RACE-FIX: Do NOT clear _ended_sessions here. The flag is cleared
                # by send_to_special_agent when a NEW turn starts. Clearing it here
                # (inside _do_text_delta) was the original race bug: a stale delta
                # arriving after completion would clear the flag and start a new bubble.
                # NEW: drawer-lifecycle start → drawer separator
                if self._on_drawer_lifecycle is not None:
                    agent_def_dl = self._agents.get(session_key)
                    agent_name_dl = agent_def_dl.display_name if agent_def_dl else "Agent"
                    self._on_drawer_lifecycle(session_key, agent_name_dl, "start")

        # Render the producer-accumulated text unconditionally (the throttle
        # was applied by the scheduler before this dispatch was queued).
        self._crh.update_streaming(session_key, self._streaming_text.get(session_key, ""))

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

    def _do_tool_call_start(self, session_key: str, name: str, args: dict) -> None:
        """Main-thread portion of _on_tool_call_start.

        Phase D: Create an agent_action feed card with running state.
        """
        # BUG #2 / BUG #18: Suppress ALL tool_start dispatches that arrive while
        # the session is in the ended state. We do NOT clear the flag here —
        # clearing on the first stale call let a second stale call proceed
        # (BUG #18). The flag is cleared ONLY by send_to_special_agent when a
        # NEW turn starts (RACE-FIX v4), so a genuine new turn's tool_starts
        # are never suppressed. Tool-only turns are covered by the runtime's
        # on_turn_start dispatch (BUG #21 redesign) — the old "Known limitation
        # (BUG #14)" no longer applies.
        if session_key in self._ended_sessions:
            logger.debug("_do_tool_call_start: suppressed for ended session %s", session_key)
            return

        # Resolve agent name BEFORE the project guard — bubble emissions need it.
        agent_def = self._agents.get(session_key)
        agent_name = agent_def.display_name if agent_def else "Agent"

        # BUG #4: Only the feed-card logic needs _active_project; bubble emissions
        # and _pending_tool_args are moved outside this guard so they fire even
        # when no project is open (the drawer works offline).
        if self._fh is not None and self._active_project is not None:
            project_name, _ = self._active_project

            # Build human-readable title from tool name and args
            if name == "read_file":
                title = f"{agent_name} is reading {args.get('path', '?')}"
            elif name == "write_file":
                title = f"{agent_name} is writing {args.get('path', '?')}"
            elif name == "exec_command":
                cmd = args.get("command", "?")
                title = f"{agent_name} is running: {cmd[:60]}"
            elif name == "list_files":
                title = f"{agent_name} is listing {args.get('path', '.')}"
            elif name == "search_files":
                title = f"{agent_name} is searching for \"{args.get('pattern', '?')}\""
            elif name == "web_search":
                title = f"{agent_name} is searching the web"
            elif name == "web_fetch":
                title = f"{agent_name} is fetching {args.get('url', '?')[:50]}"
            else:
                title = f"{agent_name} is calling {name}"

            from models.feed_card import FeedCardData
            card = FeedCardData(
                card_type="agent_action",
                source="agent",
                title=title,
                body="⏳ Running...",  # replaced when result arrives
                author=agent_name,
                timestamp=datetime.now(timezone.utc),
                project_name=project_name,
                metadata={
                    "tool_name": name,
                    "tool_args": args,
                    "session_key": session_key,
                    "status": "running",
                },
            )
            card_id = self._fh.add_card(card)
            # Store so _do_tool_call_result can update the card
            self._tool_card_ids[session_key] = card_id

        # BUG #4: Store args and emit tool_start bubble unconditionally
        # (outside the feed-card guard). These do NOT need _active_project.
        self._pending_tool_args[session_key] = args

        # BUG #15: capture exec command unconditionally (outside the project guard)
        # so the command_output drawer row has the command text even with no project open.
        if name == "exec_command":
            self._pending_exec_commands[session_key] = args.get("command", "")

        # NEW: activity-drawer tool_start bubble
        if self._on_activity_bubble is not None:
            from models.activity import ActivityBubble, ToolStatus
            self._emit_activity_bubble(ActivityBubble(
                type="tool_start",
                session_key=session_key,
                tool_name=name,
                icon="🔧",
                status=ToolStatus.RUNNING,
                agent_name=agent_name,
            ))

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

    def _do_tool_call_result(self, session_key: str, name: str, result: Any, success: bool = True) -> None:
        """Main-thread portion of _on_tool_call_result.

        Phase D: Update the feed card with the tool result, then flag for review.
        Phase 1.5 review staging: If write_file succeeds and review mode is active,
        copy the written file to a shadow staging directory inside the project.
        """
        logger.debug("[handler] _do_tool_call_result: sk=%s tool=%s result_len=%d",
                     session_key, name, len(str(result)) if result else 0)
        # Phase D: Update the feed card with the result
        card_id = self._tool_card_ids.pop(session_key, None)
        if card_id is not None and self._fh is not None:
            stored = self._fh.get_card(card_id)
            if stored is not None:
                # Work on a COPY (audit: mutate-before-persist). Every mutation
                # below belongs to the card's NEW state; applying it to the
                # store's object first would leave memory claiming an update
                # that a no-op persist never recorded. update_card performs the
                # in-memory replacement from this copy.
                # metadata is replaced with a shallow copy too, so the store's
                # dict is not aliased while the flag/status are set below.
                # `is_dataclass` guard keeps non-dataclass test doubles working
                # (stubs return MagicMocks from get_card); production always
                # gets a real FeedCardData here.
                if is_dataclass(stored):
                    card = replace(stored, metadata=dict(stored.metadata or {}))
                else:  # pragma: no cover - test-double path
                    card = stored
                # Extract output from result (ToolResult or string)
                if hasattr(result, 'output'):
                    output_text = result.output or ""
                    error_text = result.error or ""
                    card_success = result.success
                    duration = getattr(result, 'duration_ms', 0)
                else:
                    output_text = str(result) if result else ""
                    error_text = ""
                    # BUG #17: use the runtime-dispatched param, not a hard-coded True.
                    # A string result with success=False is a denied/failed tool — the
                    # card must agree with the bubble's tool_error classification.
                    card_success = success
                    duration = 0

                # UIRESP2-T2 Edit C: store the FULL tool output. The 2,000-char
                # limit is a RENDER concern (ui/views/feed_card.py); truncating
                # here made the stored text lossy, so copy/crabcard/audit saw a
                # silent 2,000-char prefix. Only the large storage cap applies,
                # and it warns when it fires.
                display = output_text or ""
                if error_text:
                    display = f"❌ {error_text}\n{display}"
                display = cap_stored_body(display, site=f"tool_result:{name}")

                card.body = display
                card.metadata["status"] = "complete" if card_success else "error"
                card.metadata["duration_ms"] = duration

                # Flag for review if write_file/exec_command in active review session
                if name in ("write_file", "exec_command") and self._review_handler is not None:
                    proj_name, proj_path = self._active_project or (None, None)
                    if proj_path:
                        state = self._review_handler.get_state(proj_name)
                        if state and state.is_active():
                            card.metadata["needs_review"] = True

                # Persist the updated card data and re-render the widget
                self._fh.update_card(card_id, card)

        # SPEC-activity-drawer §2.5: fire command_output callback for exec_command.
        # Captures the command from start time, takes the last 10 lines of output
        # from the ToolResult, plus exit_code and duration_ms for the drawer's
        # exit badge and duration display. Output may be empty for silent commands.
        if name == "exec_command" and self._on_command_output is not None:
            cmd = self._pending_exec_commands.pop(session_key, "")
            output_text = ""
            if hasattr(result, "output") and result.output:
                output_text = result.output
            elif isinstance(result, str):
                output_text = result
            # Extract exit_code (ToolResult.exit_code is int | None; default to 0)
            exit_code = getattr(result, "exit_code", 0) or 0
            # Extract duration_ms (ToolResult.duration_ms is int; default to 0)
            duration_ms = getattr(result, "duration_ms", 0) or 0
            # Tail to last 10 lines (matches drawer's OUTPUT_LINE_CAP)
            lines = output_text.splitlines()
            tail = "\n".join(lines[-10:]) if lines else ""
            self._on_command_output(session_key, cmd, tail, exit_code, duration_ms)

        # NEW: activity-drawer tool_end/tool_error bubble (all tools EXCEPT write_file)
        # BUG #12: write_file success gets only a patch bubble (matching gateway).
        # write_file failure still emits tool_error.
        if self._on_activity_bubble is not None:
            from models.activity import ActivityBubble, ToolStatus
            # BUG #1: Use the success param from the runtime dispatch, not hasattr on string.
            is_error = not success
            duration_ms_bubble = getattr(result, "duration_ms", 0) or 0
            agent_def_bubble = self._agents.get(session_key)
            agent_name_bubble = agent_def_bubble.display_name if agent_def_bubble else "Agent"
            # BUG #12: skip tool_end for write_file (patch bubble covers it),
            # but DO emit tool_error for failed write_file.
            skip_tool_end = (name == "write_file" and not is_error)
            if not skip_tool_end:
                self._emit_activity_bubble(ActivityBubble(
                    type="tool_error" if is_error else "tool_end",
                    session_key=session_key,
                    tool_name=name,
                    duration_ms=duration_ms_bubble,
                    icon="❌" if is_error else "✅",
                    status=ToolStatus.ERROR if is_error else ToolStatus.SUCCESS,
                    agent_name=agent_name_bubble,
                ))

        # BUG #5: Pop _pending_tool_args unconditionally after the bubble dispatch.
        # This runs for ALL tools, not just write_file — prevents leaks from
        # failed read_file, search_files, etc.
        args_write = self._pending_tool_args.pop(session_key, {})

        # Phase 1.5 review staging — if write_file succeeds and review is active,
        # copy the written file to a shadow staging directory so the PM can Accept/Reject.
        # Also emit patch bubble for write_file success (BUG #12: only patch, no tool_end).
        write_file_success = (name == "write_file"
                              and isinstance(result, str)
                              and result.startswith("OK"))
        if not write_file_success:
            return

        # NEW: activity-drawer patch bubble for write_file success
        # (suppressed tool_end already handled above via BUG #12)
        if self._on_activity_bubble is not None:
            from models.activity import ActivityBubble, ToolStatus
            agent_def_bubble = self._agents.get(session_key)
            agent_name_bubble = agent_def_bubble.display_name if agent_def_bubble else "Agent"
            file_path = args_write.get("path", "") if isinstance(args_write, dict) else ""
            self._emit_activity_bubble(ActivityBubble(
                type="patch",
                session_key=session_key,
                tool_name="write_file",
                file_path=file_path,
                modified=1,
                icon="✏️",
                status=ToolStatus.SUCCESS,
                agent_name=agent_name_bubble,
            ))

        proj_name, proj_path = self._active_project or (None, None)
        if proj_path is None or self._review_handler is None:
            return

        state = self._review_handler.get_state(proj_name)
        if state is None or not state.is_active():
            return

        # Extract relative path from output like "OK — wrote 123 bytes to src/foo.py"
        path_match = re.search(r"to (.+)$", result)
        if not path_match:
            return
        rel_path = path_match.group(1).strip()

        from agent.config import load_agent_config
        cfg = load_agent_config()
        staging_dir = os.path.join(proj_path, cfg.review_staging_dirname)
        os.makedirs(staging_dir, exist_ok=True)
        real_path = os.path.join(proj_path, rel_path)
        staging_path = os.path.join(staging_dir, rel_path)
        os.makedirs(os.path.dirname(staging_path), exist_ok=True)
        shutil.copy2(real_path, staging_path)
        logger.info("Review staging: copied %s → %s", real_path, staging_path)

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

    def _do_response_complete(self, session_key: str, text: str, complete_token: object = None) -> None:
        """Main-thread portion of _on_response_complete.

        Phase B: Prevent duplicate bubbles.
        Phase C: Extract crabcard blocks from streaming text and route to the feed.

        When streaming was active: extract crabcards from sb.plain_text BEFORE
        end_streaming(), then overwrite sb.plain_text with cleaned text so
        _finalize() renders the bubble without crabcard blocks.

        When streaming was not active: extract from the text arg (non-streaming path).

        Header fix: local special agents are NOT in AgentManager (only gateway
        agents are — see gateway_handler.on_connected). Their display name
        comes from self._agents, populated by add_special_agent(). We resolve
        it once here and thread it into both end_streaming and render_sync so
        build_role_bubble's header condition (chat_bubble.py:284) is satisfied.
        Without this, local agent bubbles render the body but no name/dot/timestamp.
        """
        # RACE-FIX v4: Reject stale completions from a previous turn.
        # complete_token=None means a legacy caller (no token) — allow through.
        if complete_token is not None:
            current_token = self._turn_tokens.get(session_key)
            if complete_token is not current_token:
                logger.debug("_do_response_complete: stale completion (token mismatch) for %s, skipping", session_key)
                return

        # RACE-FIX v4: Mark session ended + completed BEFORE any rendering
        # work or early returns. This ensures:
        # 1. Stale deltas see the flag (regardless of idle ordering)
        # 2. Duplicate completion is prevented (boolean, not counter-based)
        # 3. Even if _crh is None, the flags are set (fixes early-return gap)
        self._ended_sessions.add(session_key)
        if session_key in self._session_completed:
            logger.debug("_do_response_complete: duplicate completion for %s, skipping", session_key)
            return
        self._session_completed.add(session_key)

        if self._crh is None:
            return

        # Clear accumulated streaming text — no longer needed
        self._streaming_text.pop(session_key, None)
        self._last_delta_dispatch.pop(session_key, None)
        # AC3 Part A: drop any pending coalesced dispatch + dirty flag — the
        # turn is over; leftovers would suppress scheduling on the next turn.
        self._delta_dispatch_pending.discard(session_key)
        self._delta_dirty.discard(session_key)

        was_streaming = self._crh.is_streaming(session_key)
        project_name = self._active_project[0] if self._active_project else None

        logger.debug("[handler] _do_response_complete: sk=%s was_streaming=%s text_len=%d",
                     session_key, was_streaming, len(text or ""))

        # Resolve the agent's display name from the local agent registry.
        # None for unregistered session_keys (defensive — fallback in
        # end_streaming / render_sync uses agent_mgr.get_name which works
        # for gateway agents).
        agent_def = self._agents.get(session_key)
        resolved_name = agent_def.display_name if agent_def else None

        # RACE-FIX: The authoritative full text is the `text` argument from the
        # runtime (the complete LLM response). sb.plain_text may be stale if the
        # handler's throttle skipped update_streaming calls for later chunks.
        # Overwrite sb.plain_text with the full text BEFORE crabcard extraction
        # so _finalize always renders the complete message.
        if was_streaming and text:
            self._crh.set_streaming_text(session_key, text)

        # Phase C: Extract crabcards from the authoritative text before end_streaming
        if was_streaming and project_name and self._fh is not None:
            from utils.crabcard_parser import extract_crabcards
            full_text = self._crh.get_streaming_text(session_key) or ""
            if full_text:
                cleaned, cards = extract_crabcards(full_text, project_name, "Special Agent")
                if cards:
                    # Batch all cards from one response into a single main-thread
                    # pass — avoids N idle callbacks racing the vadjustment.
                    for card_data in cards:
                        card_data.project_name = project_name
                    self._fh.add_cards_batch(cards)
                    # Overwrite streaming text with cleaned version so
                    # end_streaming._finalize renders the bubble without crabcard blocks
                    self._crh.set_streaming_text(session_key, cleaned)

        # Phase B: end_streaming() finalizes the bubble (uses current sb.plain_text).
        # Pass resolved_name so local special agents get their header.
        # BUG #22: if the streaming text is empty (tool-only turn where the BUG #21
        # empty-delta started a bubble but no content arrived), suppress the final
        # bubble render — the streaming widget is cleaned up, but no empty header
        # bubble is created. end_streaming's render=False does the cleanup only.
        streaming_text = self._crh.get_streaming_text(session_key) or ""
        self._crh.end_streaming(
            session_key,
            agent_name=resolved_name,
            render=bool(streaming_text.strip()),
        )

        # Non-streaming fallback: render from text argument with crabcard extraction
        # Defensive: if response completed with empty text and no streaming bubble,
        # render a fallback message so the user sees feedback instead of silence.
        if not was_streaming and not text:
            chat_box = self._resolve_chat_box(session_key)
            if chat_box is not None:
                fallback_text = "⚠️ Agent returned no content. This may indicate a configuration error or an issue with the LLM provider."
                bubble = self._crh.render_sync(
                    "System", fallback_text, session_key, agent_name="System"
                )
                if bubble is not None:
                    chat_box.append(bubble)
                self._mc.scroll_chat_to_bottom()

        if not was_streaming and text:
            if project_name and self._fh is not None:
                from utils.crabcard_parser import extract_crabcards
                cleaned, cards = extract_crabcards(text, project_name, "Special Agent")
                if cards:
                    # Batch: single idle callback, single smart scroll
                    for card_data in cards:
                        card_data.project_name = project_name
                    self._fh.add_cards_batch(cards)
                text_for_bubble = cleaned if cards else text
            else:
                text_for_bubble = text

            chat_box = self._resolve_chat_box(session_key)
            if chat_box is not None:
                bubble = self._crh.render_sync(
                    "Agent", text_for_bubble, session_key, agent_name=resolved_name or "Agent"
                )
                if bubble is not None:
                    chat_box.append(bubble)
                self._mc.scroll_chat_to_bottom()

        # Agent command parsing hook (Phase 6.2) — fire after bubble render, before lifecycle
        if self._on_agent_response is not None and text:
            project_name = self._active_project[0] if self._active_project else None
            self._on_agent_response(session_key, text, project_name)

        # Fire lifecycle: agent finished → ActivityHandler progress bar
        if self._on_agent_end_cb:
            self._on_agent_end_cb(session_key)
        # Phase 4 Part C: drain this session's batched bubbles BEFORE the
        # drawer's end separator, so a queued tool row can never render
        # underneath the separator that follows it.
        self.flush_pending_activity_bubbles(session_key)
        # NEW: drawer-lifecycle end → drawer separator
        if self._on_drawer_lifecycle is not None:
            agent_def_dl = self._agents.get(session_key)
            agent_name_dl = agent_def_dl.display_name if agent_def_dl else "Agent"
            self._on_drawer_lifecycle(session_key, agent_name_dl, "end")
        # _ended_sessions is now set at the TOP of _do_response_complete (line 1454)
        # for race-safety. This duplicate add is harmless (idempotent) but kept
        # for documentation of the lifecycle endpoint.

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
            "Agent", text, session_key, agent_name=None
        )
        if bubble is not None:
            chat_box.append(bubble)
            self._mc.scroll_chat_to_bottom()
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
            )
            bubble = self._crh.render_sync(
                "Agent", text, session_key, agent_name=None
            )
            if bubble is not None:
                chat_box.append(bubble)
                self._mc.scroll_chat_to_bottom()

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

    def _do_error(self, session_key: str, message: str | BaseException, error_token: object = None) -> None:
        """Main-thread portion of _on_error.

        ``message`` mirrors OnError's contract (agent/callbacks.py): either a
        user-friendly string or the raw exception object — provider errors
        arrive as exceptions so _last_error_exception can enrich the display.
        """
        # RACE-FIX v4: Reject stale errors from a previous turn.
        if error_token is not None:
            current_token = self._turn_tokens.get(session_key)
            if error_token is not current_token:
                logger.debug("_do_error: stale error (token mismatch) for %s, skipping", session_key)
                return
        # RACE-FIX v4: Mark session ended + completed (same as _do_response_complete).
        self._ended_sessions.add(session_key)
        if session_key in self._session_completed:
            logger.debug("_do_error: duplicate completion for %s, skipping", session_key)
            return
        self._session_completed.add(session_key)

        logger.debug("[handler] _do_error: sk=%s msg=%s", session_key, message)
        self._streaming_text.pop(session_key, None)
        self._last_delta_dispatch.pop(session_key, None)
        # AC3 Part A: drop any pending coalesced dispatch + dirty flag — the
        # turn is over; leftovers would suppress scheduling on the next turn.
        self._delta_dispatch_pending.discard(session_key)
        self._delta_dirty.discard(session_key)
        # When the runtime passes a raw exception object (not a string),
        # translate it to a user-friendly message for display while keeping
        # the exception stored in _last_error_exception for context enrichment.
        if isinstance(message, BaseException):
            from agent.llm.streaming import friendly_error_message
            display_msg = friendly_error_message(message)
        else:
            display_msg = str(message)
        # Resolve agent display name from the local registry so the error
        # bubble header shows "Coder" / "Debugger" / etc. instead of "Agent".
        # Mirrors the resolution in _do_response_complete.
        agent_def = self._agents.get(session_key)
        resolved_name = agent_def.display_name if agent_def else None
        if self._crh is not None:
            # BUG #22 guard (same pattern as _do_response_complete): on a
            # tool-only turn the streaming bubble exists but holds no text —
            # end_streaming must clean up WITHOUT rendering an empty bubble.
            streaming_text = self._crh.get_streaming_text(session_key) or ""
            self._crh.end_streaming(
                session_key,
                agent_name=resolved_name,
                render=bool(streaming_text.strip()),
            )
            chat_box = self._resolve_chat_box(session_key)
            if chat_box is not None:
                rendered = f"[Error] {display_msg}"
                try:
                    exc_obj = self._last_error_exception.get(session_key)
                    if exc_obj is not None:
                        ctx = getattr(exc_obj, "_crabcakes_context", None)
                        if ctx:
                            rendered += f"\nProvider: {ctx.get('provider')} | Model: {ctx.get('model')}"
                except Exception:
                    pass
                bubble = self._crh.render_sync(
                    "Agent", rendered, session_key, agent_name=resolved_name or "Agent"
                )
                if bubble is not None:
                    chat_box.append(bubble)
                self._mc.scroll_chat_to_bottom()

        # SPEC-02: turn-fatal errors surface in the Project Feed too, not just
        # chat + stderr. Same guard pattern as publish_cli_nudge_card (:538):
        # the card is skipped only when no feed handler is wired (headless),
        # never because no project is active — project_name falls back to
        # "(none)" exactly like the nudge card.
        #
        # SPEC-02 fix round (audit #2/#3): the WHOLE card block is
        # best-effort. The original narrow try covered only the context
        # lookup, so a non-dict `_crabcakes_context` attachment
        # (AttributeError on `.get`) or a raising `add_card` (lock
        # contention / mid-shutdown) escaped `_do_error` entirely — no card,
        # and the `_on_agent_end_cb` lifecycle fire below never ran, leaving
        # the activity drawer stuck on "running". Everything — metadata
        # read, card construction, add_card — is now inside one guard;
        # a failure logs and falls through to the lifecycle fire.
        if self._fh is not None:
            try:
                from agent.runtime import CANCEL_MESSAGE
                from models.feed_card import FeedCardData
                provider_meta = None
                exc_obj = self._last_error_exception.get(session_key)
                if exc_obj is not None:
                    ctx = getattr(exc_obj, "_crabcakes_context", None)
                    # Audit #2: the runtime attaches this as a dict, but a
                    # truthy non-dict (corrupt attachment) must not escape —
                    # treat anything but a dict as absent.
                    provider_meta = ctx if isinstance(ctx, dict) else None
                # SPEC-02 fix round (audit #4): a deliberate user cancel is
                # not a turn-fatal provider error — no "Turn failed" card
                # (stop-all in SPEC-09 would otherwise spray one per agent).
                # Compared against the runtime's constant, never a bare
                # string, so the two sides can't drift.
                if display_msg != CANCEL_MESSAGE:
                    card = FeedCardData(
                        card_type="system",
                        source="agent",
                        title=f"Turn failed: {resolved_name or self.get_agent_name_for_session(session_key) or 'Agent'}",
                        body=display_msg[:2000],
                        author="Runtime",
                        timestamp=datetime.now(timezone.utc),  # noqa: UP017 — same pattern as publish_cli_nudge_card (:565)
                        project_name=self._active_project[0] if self._active_project else "(none)",
                        metadata={
                            "session_key": session_key,
                            "kind": "turn_error",
                            "provider": (provider_meta or {}).get("provider"),
                            "model": (provider_meta or {}).get("model"),
                            "exception_type": (provider_meta or {}).get("exception_type"),
                        },
                    )
                    self._fh.add_card(card)
            except Exception:
                logger.exception(
                    "turn-error feed card emission failed for %s (non-fatal)",
                    session_key,
                )

        # Fire lifecycle: agent finished (error) → ActivityHandler returns to idle
        if self._on_agent_end_cb:
            self._on_agent_end_cb(session_key)
        # Phase 4 Part C: drain batched bubbles before the end separator
        # (same ordering guarantee as in _do_response_complete).
        self.flush_pending_activity_bubbles(session_key)
        # NEW: drawer-lifecycle end → drawer separator (error path)
        if self._on_drawer_lifecycle is not None:
            agent_def_dl = self._agents.get(session_key)
            agent_name_dl = agent_def_dl.display_name if agent_def_dl else "Agent"
            self._on_drawer_lifecycle(session_key, agent_name_dl, "end")
        # _ended_sessions is set at the TOP of _do_error (line 1778) for race-safety.
