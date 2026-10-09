"""Turn UI adapters: deltas, tools, completion, and errors."""
from __future__ import annotations

import os
import re
import shutil
import time
from dataclasses import is_dataclass, replace
from datetime import datetime, timezone
from typing import Any

from models.feed_card import cap_stored_body
from ui.agent_runtime._facade import _mod


class RuntimeTurnUiMixin:
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
            _mod().logger.debug(
                "_on_text_delta: dropping delta for ended session %s", session_key,
            )
            return
        if _turn_token is not None:
            current = self._turn_tokens.get(session_key)
            if _turn_token is not current:
                _mod().logger.debug(
                    "_on_text_delta: dropping stale delta (token mismatch) for %s",
                    session_key,
                )
                return
        # Producer-side accumulation (was per-delta on the main thread — an
        # O(n) copy per delta flooding the main loop; audit finding #2).
        self._streaming_text[session_key] = self._streaming_text.get(session_key, "") + text
        if text and self._on_stream_delta_cb is not None:
            try:
                self._on_stream_delta_cb(session_key, text)
            except Exception:
                _mod().logger.exception("stream-delta callback failed for %s", session_key)
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
            _mod().logger.debug(
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
                _mod().logger.debug(
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
            _mod().logger.debug("_do_tool_call_start: suppressed for ended session %s", session_key)
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

        if self._on_tool_start_cb is not None:
            try:
                self._on_tool_start_cb(session_key, name)
            except Exception:
                _mod().logger.exception("tool-start callback failed for %s", session_key)

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

    def _do_tool_call_result(self, session_key: str, name: str, result: Any, success: bool = True) -> None:
        """Main-thread portion of _on_tool_call_result.

        Phase D: Update the feed card with the tool result, then flag for review.
        Phase 1.5 review staging: If write_file succeeds and review mode is active,
        copy the written file to a shadow staging directory inside the project.
        """
        _mod().logger.debug("[handler] _do_tool_call_result: sk=%s tool=%s result_len=%d",
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
        _mod().logger.info("Review staging: copied %s → %s", real_path, staging_path)

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
        build_role_bubble's header condition (event_cards.py:288) is satisfied.
        Without this, local agent bubbles render the body but no name/dot/timestamp.
        """
        # RACE-FIX v4: Reject stale completions from a previous turn.
        # complete_token=None means a legacy caller (no token) — allow through.
        if complete_token is not None:
            current_token = self._turn_tokens.get(session_key)
            if complete_token is not current_token:
                _mod().logger.debug("_do_response_complete: stale completion (token mismatch) for %s, skipping", session_key)
                return
        # SPEC-12 (R5 REV 3): bind the turn's reply key ONCE and use it at
        # every render/mount site below. The pop is turn-GUARDED (SP3b-audit
        # BUG#1): a nested send to the same session_key inside the try runs a
        # NEW turn and sets a NEW slot — the outer finally must not clobber
        # it. Capture THIS turn's token so the finally can check ownership.
        my_token = self._turn_tokens.get(session_key)
        reply_key = self._reply_key(session_key)
        try:
            # RACE-FIX v4: Mark session ended + completed BEFORE any rendering
            # work or early returns. This ensures:
            # 1. Stale deltas see the flag (regardless of idle ordering)
            # 2. Duplicate completion is prevented (boolean, not counter-based)
            # 3. Even if _crh is None, the flags are set (fixes early-return gap)
            self._ended_sessions.add(session_key)
            if session_key in self._session_completed:
                _mod().logger.debug("_do_response_complete: duplicate completion for %s, skipping", session_key)
                return
            self._session_completed.add(session_key)

            # SPEC-10 SP2b (D2 REV 2): turn-complete agent checkpoint. Fires for
            # COMPLETED turns only (this method IS the completed-turn dispatch —
            # CANCELLED/FAILED turns route to _do_error, which never calls this).
            self._maybe_agent_checkpoint(session_key, complete_token)

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

            _mod().logger.debug("[handler] _do_response_complete: sk=%s was_streaming=%s text_len=%d",
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
                            card_data.metadata["session_key"] = session_key
                            card_data.metadata["tab_key"] = (
                                reply_key)
                            # SP5b bug #5: stamp tab linkage at CONSTRUCTION —
                            # crabcards must resolve to the emitting session's tab
                            # after the window's old linkage callback died.
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
                # FIX 7 (SP5a round 2): mount_key through the STREAMING path —
                # production always streams (was_streaming always True), so the
                # final transcript row's surface must mount by the RESOLVED key
                # exactly like the render_sync fallback path already does.
                mount_key=reply_key,
            )

            # Non-streaming fallback: render from text argument with crabcard extraction
            # Defensive: if response completed with empty text and no streaming bubble,
            # render a fallback message so the user sees feedback instead of silence.
            if not was_streaming and not text:
                chat_box = self._resolve_chat_box(session_key)
                if chat_box is not None:
                    fallback_text = "⚠️ Agent returned no content. This may indicate a configuration error or an issue with the LLM provider."
                    bubble = self._crh.render_sync(
                        "System", fallback_text, session_key, agent_name="System",
                        mount_key=reply_key,
                    )
                    if bubble is not None:
                        chat_box.append(bubble)

            if not was_streaming and text:
                if project_name and self._fh is not None:
                    from utils.crabcard_parser import extract_crabcards
                    cleaned, cards = extract_crabcards(text, project_name, "Special Agent")
                    if cards:
                        # Batch: single idle callback, single smart scroll
                        for card_data in cards:
                            card_data.project_name = project_name
                            card_data.metadata["session_key"] = session_key
                            card_data.metadata["tab_key"] = (
                                reply_key)
                            # SP5b bug #5: stamp tab linkage at CONSTRUCTION —
                            # same contract as the streaming block above.
                        self._fh.add_cards_batch(cards)
                    text_for_bubble = cleaned if cards else text
                else:
                    text_for_bubble = text

                chat_box = self._resolve_chat_box(session_key)
                if chat_box is not None:
                    bubble = self._crh.render_sync(
                        "Agent", text_for_bubble, session_key, agent_name=resolved_name or "Agent",
                        mount_key=reply_key,
                    )
                    if bubble is not None:
                        chat_box.append(bubble)

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
        finally:
            # SP3b-audit BUG#1: pop ONLY this turn's slot — a nested
            # same-session send inside the try owns a NEWER token; leave it.
            # SP3b-audit BUG#1 residual: a None-token (legacy / guard-2
            # deferred) call has no independent identity — do NOT pop (the
            # next send normalizes the slot via BUG#18).
            if (complete_token is not None
                    and self._turn_tokens.get(session_key) is my_token):
                self._turn_reply_target.pop(session_key, None)

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
                _mod().logger.debug("_do_error: stale error (token mismatch) for %s, skipping", session_key)
                return
        # SPEC-12: bind once; the pop is turn-GUARDED (SP3b-audit BUG#1 — a
        # nested same-session send owns a newer token and must keep its slot).
        my_token = self._turn_tokens.get(session_key)
        reply_key = self._reply_key(session_key)
        try:
            # RACE-FIX v4: Mark session ended + completed (same as _do_response_complete).
            self._ended_sessions.add(session_key)
            if session_key in self._session_completed:
                _mod().logger.debug("_do_error: duplicate completion for %s, skipping", session_key)
                return
            self._session_completed.add(session_key)

            _mod().logger.debug("[handler] _do_error: sk=%s msg=%s", session_key, message)
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
                    # FIX 4 (SP5a r3): resolved mount key threaded like :2102.
                    mount_key=reply_key,
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
                        "Agent", rendered, session_key, agent_name=resolved_name or "Agent",
                        mount_key=reply_key,
                    )
                    if bubble is not None:
                        chat_box.append(bubble)

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
                    _mod().logger.exception(
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
        finally:
            # SP3b-audit BUG#1: pop ONLY this turn's slot (nested-send guard).
            # SP3b-audit BUG#1 residual: None-token (legacy / guard-2 deferred)
            # calls own no slot — do NOT pop.
            if (error_token is not None
                    and self._turn_tokens.get(session_key) is my_token):
                self._turn_reply_target.pop(session_key, None)

