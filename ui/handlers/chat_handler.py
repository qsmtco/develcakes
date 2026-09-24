# ui/handlers/chat_handler.py
# Chat handler — extracted from window.py Phase 1.
#
# Owns: message sending, project fan-out, incoming message routing, tab switching.
# Does NOT own: gateway connection lifecycle, project state, STT, prompt selection.
#
# Thread safety: on_chat_event() is called from the gateway background thread.
# All GTK calls (switch_to_tab, get_chat_box) MUST
# be dispatched via GLib.idle_add(). The handler receives GLib as a constructor
# argument for this purpose.
#
# If GLib is None (e.g. in tests), GTK calls are made directly — only safe
# when the caller is already on the main thread.

from typing import Callable

import logging

import ui.constants

_logger = logging.getLogger(__name__)


class ChatHandler:
    """
    Handles all chat-related logic: sending, fan-out, and routing responses.

    This handler is thread-aware: its public methods may be called from
    background threads (gateway client thread). GTK operations are dispatched
    via GLib.idle_add when a GLib module is provided.

    Args:
        main_content:       MainContent instance — for tab ops and input access
        agent_to_project   — AgentRoutingTable — ProjectHandler writes, ChatHandler reads
        projects_module:    utils.projects module — for load_members()
        GLib_module:        gi.repository.GLib or None — for thread-safe GTK calls
    """

    def __init__(
        self,
        main_content,      # MainContent
        agent_to_project,  # AgentRoutingTable — ProjectHandler writes, ChatHandler reads
        projects_module,   # module — utils.projects
        GLib_module=None,  # gi.repository.GLib or None
    ):
        self._mc = main_content
        # agent_to_project is an AgentRoutingTable — ProjectHandler writes, ChatHandler reads
        self._agent_to_project = agent_to_project
        self._projects = projects_module
        self._GLib = GLib_module
        self._project_handler = None   # injected via set_project_handler()
        self._chat_render_handler = None  # injected via set_chat_render_handler()
        self._on_forward_message = None   # injected via set_on_forward_message()
        self._command_handler = None     # injected via set_command_handler() — for slash commands
        self._on_send_initiated = None    # injected via set_on_send_initiated()
        self._pending_req_id: str | None = None  # tracks last sent req_id for res correlation
        self._on_agent_response: Callable[[str, str, str | None], None] | None = None  # agent response hook (Phase 6.2)
        self._on_res_confirmed: Callable[[str], None] | None = None  # pre-flight confirm via res
        self._agent_runtime_handler = None  # injected via set_agent_runtime_handler()
        self._agent_mgr = None  # injected via set_agent_manager() after gateway connect
        # Bug fix: state for missing message recovery (Phase 1 of SPEC-smarter-chat-ux)
        self._assistant_text_buffer: dict[str, str] = {}  # session_key → last assistant text
        self._chat_final_rendered: dict[str, bool] = {}  # session_key → True when final rendered (guard)

    def set_chat_render_handler(self, handler):
        """"Inject ChatRenderHandler. Called by window.py._build()."""
        self._chat_render_handler = handler

    def set_agent_runtime_handler(self, handler):
        """"Inject AgentRuntimeHandler. Called by window.py._build()."""
        self._agent_runtime_handler = handler

    # ── Public API ───────────────────────────────────────────────────────────

    def set_on_forward_message(self, cb):
        """Set callback for forward button: cb(text, anchor_widget)."""
        self._on_forward_message = cb
        # Propagate to render handler so streaming final bubbles also get the button
        if self._chat_render_handler is not None:
            self._chat_render_handler.set_on_forward_message(cb)

    def set_on_send_initiated(self, cb):
        """Set callback for send-initiated: cb(session_key). Called before message is sent."""
        self._on_send_initiated = cb

    def set_on_agent_response(self, cb: Callable[[str, str, str | None], None]) -> None:
        """Set callback for agent response command parsing hook (Phase 6.2).

        Called after an agent's final response is rendered, with the agent's
        session key, full response text, and active project name.
        """
        self._on_agent_response = cb

    def set_on_res_confirmed(self, cb):
        """Set callback for pre-flight res confirmation: cb(session_key)."""
        self._on_res_confirmed = cb

    def on_res_confirmed(self, session_key: str):
        """Handle gateway res — notify ActivityHandler to end pre-flight."""
        if not session_key:
            return
        if self._on_res_confirmed is not None:
            self._on_res_confirmed(session_key)

    def _buffer_assistant_text(self, session_key: str, text: str):
        """Buffer assistant text from ActivityHandler for missing-message recovery.

        Called by ActivityHandler's on_assistant_buffer callback after every
        stream=assistant event. ChatHandler uses this to recover when a chat
        final arrives with no message body.
        """
        self._assistant_text_buffer[session_key] = text

    def _clear_render_guard(self, session_key: str):
        """Clear the render guard for a session — called when a new agent round starts.

        ActivityHandler calls this via set_on_agent_start when lifecycle phase=start
        fires. Without this cleanup, the guard blocks ALL subsequent responses
        for the same session after the first render.
        """
        self._chat_final_rendered.pop(session_key, None)

    def _handle_lifecycle_completed(self, session_key: str, buffered_text: str):
        """Render fallback bubble when lifecycle ended without a chat final.

        Called by ActivityHandler's on_lifecycle_completed callback when the agent
        round-trip ends (phase=end or phase=error) but no chat final event with
        a message has arrived yet.

        Architecture: only one render per session. Uses _chat_final_rendered
        guard to prevent double-render. All rendering lives in ChatHandler.
        """
        if not session_key or not buffered_text:
            return
        # Guard: do not double-render if chat final already handled this session.
        if self._chat_final_rendered.get(session_key):
            return
        _logger.info("[fallback] Rendering fallback for session=%r (lifecycle ended)", session_key)
        # Resolve the project tab for this agent (mirrors on_chat_event routing).
        project_name = None
        if self._agent_to_project is not None:
            project_name = self._agent_to_project.get_project(session_key)
        target_tab = f"project:{project_name}" if project_name else session_key
        self._dispatch(lambda t=target_tab, sk=session_key, txt=buffered_text: (
            self._handle_final_response(t, sk, txt)
        ))

    def set_project_handler(self, handler):
        """Inject ProjectHandler. Called by window.py._build()."""
        self._project_handler = handler

    def set_command_handler(self, handler):
        """Inject CommandHandler. Called by window.py._build()."""
        self._command_handler = handler

    def set_agent_manager(self, agent_mgr) -> None:
        """Inject the live AgentManager after gateway connect. Called by window.py."""
        self._agent_mgr = agent_mgr

    def _send_local(self, session_key: str, text: str) -> None:
        """Single local send entry point - ALL send sites route through here.

        FIX 7 (SPEC-05 SP2 audit BUG #7): ONE None-guard instead of eight
        scattered ones. If the AgentRuntimeHandler is not wired yet (early
        startup, partial construction), the send is dropped with a WARNING
        instead of raising AttributeError from inside a GTK callback.
        """
        if self._agent_runtime_handler is None:
            _logger.warning(
                "[chat] Send dropped for %r - AgentRuntimeHandler not wired yet",
                session_key,
            )
            return
        # Local path only (SPEC-05 R1); the receiver no-ops with a warning
        # for unregistered/remote keys.
        self._agent_runtime_handler.send_to_special_agent(session_key, text)

    def _show_forward_menu(self, text, anchor_widget):
        """
        Show a popover listing other agents to forward text to.
        Called by bubble forward button via set_on_forward_message.
        """
        if self._on_forward_message:
            self._on_forward_message(text, anchor_widget)

    def on_send_clicked(self, _btn=None):
        """GTK signal handler for the Send button. Delegates to on_send."""
        self.on_send()

    def on_send(self):
        """
        Read input text, display it in the current tab, send it to the gateway.

        If the current tab is a project tab ("project:<name>"), fan-out to all
        project members. Otherwise send directly to the single agent session.

        If input starts with the command prefix and CommandHandler is wired, the
        command is parsed and executed — gateway send is skipped.
        """
        session_key = self._mc.get_current_session_key()
        if session_key is None:
            return

        buf = self._mc.user_input.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True).strip()
        if not text:
            return

        # ── Command handler check (must run before special-agent and gateway
        #    branches so slash commands like /clear work in ALL tabs) ────────
        if self._command_handler is not None:
            result = self._command_handler.process_input(session_key, text)
            if result.handled:
                buf.set_text("")
                # Forward-to commands: show echo and route via gateway
                if result.forward_to and result.forward_text:
                    agent_name = result.forward_to.split("/")[-1]
                    echo_text = f"→ @{agent_name}: {result.forward_text}"
                    def _show_echo_and_forward():
                        chat_box = self._mc.get_chat_box()
                        if chat_box is not None:
                            if self._chat_render_handler is not None:
                                def _on_bubble(bubble):
                                    if bubble is not None:
                                        chat_box.append(bubble)
                                    self._mc.scroll_chat_to_bottom()
                                self._chat_render_handler.render_async(
                                    "You", echo_text, session_key,
                                    on_bubble_ready=_on_bubble,
                                    on_forward_click=self._on_forward_message,
                                    agent_name="You",
                                )
                        # Route through AgentRuntime for special agents, gateway for others
                        # Local path only (SPEC-05 R1): the receiver no-ops
                        # with a warning for unregistered/remote keys.
                        self._send_local(result.forward_to, result.forward_text)
                    self._dispatch(_show_echo_and_forward)
                elif result.broadcast_targets and result.forward_text:
                    # BUG #4 fix: fan-out @ broadcast to all project members
                    echo_text = f"→ @all: {result.forward_text}"
                    def _show_broadcast_and_forward():
                        chat_box = self._mc.get_chat_box()
                        if chat_box is not None:
                            if self._chat_render_handler is not None:
                                def _on_bubble(bubble):
                                    if bubble is not None:
                                        chat_box.append(bubble)
                                    self._mc.scroll_chat_to_bottom()
                                self._chat_render_handler.render_async(
                                    "You", echo_text, session_key,
                                    on_bubble_ready=_on_bubble,
                                    on_forward_click=self._on_forward_message,
                                    agent_name="You",
                                )
                        for target in result.broadcast_targets:
                            # Local path only (SPEC-05 R1); receiver no-ops for
                            # unregistered/remote keys.
                            self._send_local(target, result.forward_text)
                    self._dispatch(_show_broadcast_and_forward)
                # Note: response_text is handled by CommandHandler._dispatch_result()
                # via on_display_text callback. No need to render it here.
                # (Previous elif block caused double-bubble bug — both paths fired.)
                return
            # Not a command (handled=False) — fall through to special-agent / gateway

        # ── Special agent check (Phase 1.4) ─────────────────────────────────────
        if (self._agent_runtime_handler is not None
                and session_key in self._agent_runtime_handler.get_special_agents()):
            def _show_and_route_to_agent():
                chat_box = self._mc.get_chat_box()
                if chat_box is not None:
                    if self._chat_render_handler is not None:
                        def _on_bubble(bubble):
                            if bubble is not None:
                                chat_box.append(bubble)
                            self._mc.scroll_chat_to_bottom()
                        self._chat_render_handler.render_async(
                            "You", text, session_key,
                            on_bubble_ready=_on_bubble,
                            on_forward_click=self._on_forward_message,
                            agent_name="You",
                        )
                self._send_local(session_key, text)
            self._dispatch(_show_and_route_to_agent)
            buf.set_text("")
            if self._on_send_initiated:
                self._on_send_initiated(session_key)
            return

        # ── (SPEC-05 R6): gateway-availability guard removed — the local
        # runtime path is always "connected"; the receiver handles
        # agent-not-found itself.

        # ── Inline @mention routing (project tabs only) ─────────────────────
        # After command handler returned handled=False, check for @mentions in
        # plain text. Only applies to project tabs — non-project tabs skip this.
        if session_key.startswith("project:") and self._command_handler is not None:
            from models.command import MentionResolution
            resolution = self._command_handler.resolve_inline_mention(text, session_key)
            if resolution.error:
                # Show error as chat text
                buf.set_text("")
                def _show_error():
                    chat_box = self._mc.get_chat_box()
                    if chat_box is not None and self._chat_render_handler is not None:
                        bubble = self._chat_render_handler.render_sync("System", resolution.error, session_key)
                        if bubble is not None:
                            chat_box.append(bubble)
                        self._mc.scroll_chat_to_bottom()
                self._dispatch(_show_error)
                return
            elif resolution.target_session_key:
                # Solo send to one agent
                buf.set_text("")
                forward_text = resolution.clean_text
                agent_name = resolution.target_session_key.split("/")[-1]
                echo_text = f"\u2192 @{agent_name}: {forward_text}" if forward_text else f"\u2192 @{agent_name}"
                def _show_and_route_solo():
                    chat_box = self._mc.get_chat_box()
                    if chat_box is not None:
                        if self._chat_render_handler is not None:
                            def _on_bubble(bubble):
                                if bubble is not None:
                                    chat_box.append(bubble)
                                self._mc.scroll_chat_to_bottom()
                            self._chat_render_handler.render_async(
                                "You", echo_text, session_key,
                                on_bubble_ready=_on_bubble,
                                on_forward_click=self._on_forward_message,
                                agent_name="You",
                            )
                    # Special agents route through AgentRuntimeHandler, not gateway
                    # (they have no gateway session; gateway would silently drop the message)
                    # Local path only (SPEC-05 R1); receiver no-ops for
                    # unregistered/remote keys.
                    self._send_local(resolution.target_session_key, forward_text)
                self._dispatch(_show_and_route_solo)
                if self._on_send_initiated:
                    self._on_send_initiated(session_key)
                return
            elif resolution.is_broadcast:
                # @ broadcast to all project members
                buf.set_text("")
                forward_text = resolution.clean_text
                echo_text = f"\u2192 @all: {forward_text}" if forward_text else "\u2192 @all"
                def _show_and_route_broadcast():
                    chat_box = self._mc.get_chat_box()
                    if chat_box is not None:
                        if self._chat_render_handler is not None:
                            def _on_bubble(bubble):
                                if bubble is not None:
                                    chat_box.append(bubble)
                                self._mc.scroll_chat_to_bottom()
                            self._chat_render_handler.render_async(
                                "You", echo_text, session_key,
                                on_bubble_ready=_on_bubble,
                                on_forward_click=self._on_forward_message,
                                agent_name="You",
                            )
                    for target in resolution.broadcast_targets:
                        # Local path only (SPEC-05 R1); receiver no-ops for
                        # unregistered/remote keys.
                        self._send_local(target, forward_text)
                self._dispatch(_show_and_route_broadcast)
                if self._on_send_initiated:
                    self._on_send_initiated(session_key)
                return
            # No @mention found in text — fall through to normal send below

        # ── Normal send (not a command) ──────────────────────────────────────────

        # Display bubble AND send message (both happen in same dispatch)
        def _show_and_send():
            chat_box = self._mc.get_chat_box()
            if chat_box is not None:
                if self._chat_render_handler is not None:
                    def _on_bubble(bubble):
                        if bubble is not None:
                            chat_box.append(bubble)
                        self._mc.scroll_chat_to_bottom()
                    self._chat_render_handler.render_async(
                        "You", text, session_key,
                        on_bubble_ready=_on_bubble,
                        on_forward_click=self._on_forward_message,
                        agent_name="You",
                    )
            if session_key.startswith("project:"):
                project_name = session_key.split(":", 1)[1]
                # Check for solo DM target (set by right-click → "Member name" menu)
                if self._project_handler is not None:
                    solo_target = self._project_handler.get_solo_target(project_name)
                else:
                    solo_target = None

                if solo_target:
                    # Solo DM — send only to the selected member
                    # Special agents route through AgentRuntimeHandler, not gateway
                    # Local path only (SPEC-05 R1); receiver no-ops for
                    # unregistered/remote keys.
                    self._send_local(solo_target, text)
                else:
                    # Group broadcast — fan out to all members
                    if self._project_handler:
                        members = self._project_handler.get_project_members(project_name)
                    elif self._projects:
                        members = list(self._projects.load_members(project_name))
                    else:
                        members = []
                    for member in members:
                        # Local path only (SPEC-05 R1); receiver no-ops for
                        # unregistered/remote keys.
                        self._send_local(member, text)
            else:
                # Local path only (SPEC-05 R1); receiver no-ops for
                # unregistered/remote keys.
                self._send_local(session_key, text)
        self._dispatch(_show_and_send)
        buf.set_text("")

        # Trigger Pre Flight state in ActivityHandler (FeedBar status bar)
        if self._on_send_initiated:
            self._on_send_initiated(session_key)

    def on_chat_event(self, event: str, payload: dict):
        """
        Handle incoming gateway events.

        Event types:
          - "chat" (delta)   → update streaming bubble
          - "chat" (final)   → end streaming + render final bubble
          - Other events      → ignored

        Routing logic (for chat events):
        - If the sending agent is a known project member, switch to and display
          in the project tab.
        - Otherwise, switch to and display in the agent's own tab.

        Called from: gateway background thread. GTK calls are dispatched via
        GLib.idle_add when available.
        """
        session_key = payload.get("sessionKey", "") or ""
        project_name = self._agent_to_project.get_project(session_key)
        # Route to project tab if agent is a member, otherwise to their personal tab.
        # Project tab keys are "project:<name>" — see ProjectHandler.open_project().
        target_tab = f"project:{project_name}" if project_name else session_key

        if event != "chat":
            # Handle Phase 4 special event cards
            special_events = ("file_read", "edit_proposal", "tool_call", "error", "thinking")
            if event in special_events:
                self._dispatch(lambda e=event, sk=session_key, tt=target_tab, pl=payload: (
                    self._handle_special_event(e, sk, tt, pl)
                ))
            return

        state = payload.get("state", "")
        msg_obj = payload.get("message", {})

        if state == "delta":
            delta_text = self._extract_text(msg_obj)
            if not delta_text:
                return
            _logger.debug("[tab-dot] on_chat_event (delta): session_key=%r, target_tab=%r, state=delta", session_key, target_tab)
            self._dispatch(lambda sk=session_key, tt=target_tab, txt=delta_text: (
                self._handle_streaming_delta(sk, tt, txt)
            ))
        elif state == "final":
            final_text = self._extract_text(msg_obj)
            if not final_text:
                return
            # session_key: bubble teardown key; target_tab: UI switch + chat box
            _logger.debug("[tab-dot] on_chat_event (final): session_key=%r, target_tab=%r", session_key, target_tab)
            self._dispatch(lambda t=target_tab, sk=session_key, txt=final_text: (
                self._handle_final_response(t, sk, txt)
            ))

    def _handle_streaming_delta(self, session_key: str, target_tab: str, delta_text: str):
        """
        Update the streaming bubble for session_key with the latest delta text.

        Controlled by STREAMING_ENABLED. When disabled, delta events are ignored
        and the message appears only when the final event arrives.
        """
        if not ui.constants.STREAMING_ENABLED:
            return  # streaming disabled — wait for final event
        if self._chat_render_handler is None:
            return
        if not self._chat_render_handler.is_streaming(session_key):
            chat_box = self._mc.get_chat_box_for_session(target_tab)
            if chat_box is not None:
                self._chat_render_handler.start_streaming(session_key, chat_box, "Agent")
        self._chat_render_handler.update_streaming(session_key, delta_text)
        # If this tab is not currently visible, show unread indicator.
        current_sk = self._mc.get_current_session_key()
        _logger.debug("[tab-dot] _handle_streaming_delta: session_key=%r, target_tab=%r, current=%r, will_increment=%r", session_key, target_tab, current_sk, target_tab != current_sk)
        if target_tab != current_sk:
            self._mc.increment_unread(target_tab)

    def _handle_final_response(self, tab: str, session_key: str, final_text: str):
        """
        End any streaming bubble (if one exists) and record the message.

        When STREAMING_ENABLED is False, no streaming bubble is ever started,
        so end_streaming() is a no-op. In that case we fall back to render_sync()
        to create the final bubble directly.

        Architecture: _chat_final_rendered guard prevents double-render from both
        the chat final path and the lifecycle-completed fallback path.
        """
        # Guard: do not double-render if chat final already handled this session.
        if self._chat_final_rendered.get(session_key):
            return
        self._chat_final_rendered[session_key] = True

        current_sk = self._mc.get_current_session_key()
        chat_box = self._mc.get_chat_box_for_session(tab)
        # Always record the message in the chat box (data plane), regardless of
        # render handler state. render_sync / end_streaming are presentation.
        # If this tab is not currently visible, increment unread count so the
        # tab label dot turns yellow to signal pending messages.
        if tab != current_sk:
            self._mc.increment_unread(tab)

        if self._chat_render_handler is None:
            return
        if self._chat_render_handler.is_streaming(session_key):
            self._chat_render_handler.end_streaming(session_key)
        else:
            # No streaming bubble existed (STREAMING_ENABLED=False or first msg):
            # render the final bubble via render_sync instead.
            bubble = self._chat_render_handler.render_sync("Agent", final_text, session_key, on_forward_click=self._on_forward_message, tab_key=tab)
            if bubble is not None and chat_box is not None:
                chat_box.append(bubble)
                self._mc.scroll_chat_to_bottom()

        # Agent command parsing hook (Phase 6.2) — fire after bubble render.
        # Resolve project from routing table (authoritative for this session),
        # fall back to active project if routing table has no entry.
        if self._on_agent_response is not None and final_text:
            project_name = None
            if self._agent_to_project is not None:
                project_name = self._agent_to_project.get_project(session_key)
            if not project_name and self._project_handler is not None:
                project_name = self._project_handler.get_active_project_name()
            self._on_agent_response(session_key, final_text, project_name)

    def _extract_text(self, msg_obj) -> str:
        """Extract plain text from a gateway message object.

        The gateway sends message content in two forms:
        - A string (simple text responses)
        - A list of typed blocks (block-level formatting: code, quote, media, etc.)

        This method normalizes both into a single string for bubble rendering.
        input_image blocks (from gateway media attachments) are converted to
        ```image code blocks for the existing image rendering pipeline.
        """
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
                    # Gateway media attachment (image_generate tool / MEDIA: directive)
                    # The gateway sends type:"input_image" with image_url field.
                    # Emit as ```image code block for the existing image rendering pipeline.
                    image_url = block.get("image_url", "")
                    if image_url and isinstance(image_url, str):
                        parts.append(f"\n\n```image\n{image_url.strip()}\n```")
            return "".join(parts)
        elif isinstance(content, str):
            return content
        return str(content) if content else ""

    def _handle_special_event(self, event_type: str, session_key: str, target_tab: str, payload: dict):
        """
        Render a special event card (Phase 4: file_read, edit_proposal, tool_call, error, thinking).

        Args:
            event_type: Event name from gateway (e.g. "file_read")
            session_key: Session key for the agent/project tab
            target_tab:  Tab name for UI switching
            payload:     Full event payload dict
        """
        chat_box = self._mc.get_chat_box_for_session(target_tab)
        if chat_box is None or self._chat_render_handler is None:
            return

        msg_obj = payload.get("message", {})

        if event_type == "file_read":
            self._chat_render_handler.render_event_card(
                "file_read", chat_box,
                session_key=target_tab,
                file_path=msg_obj.get("file_path", "<unknown file>"),
                snippet=msg_obj.get("snippet", ""),
                line_range=msg_obj.get("line_range", ""),
            )
        elif event_type == "edit_proposal":
            self._chat_render_handler.render_event_card(
                "edit_proposal", chat_box,
                session_key=target_tab,
                file_path=msg_obj.get("file_path", "<unknown file>"),
                diff=msg_obj.get("diff", ""),
            )
        elif event_type == "tool_call":
            self._chat_render_handler.render_event_card(
                "tool_call", chat_box,
                session_key=target_tab,
                tool_name=msg_obj.get("tool_name", "<unknown tool>"),
                detail=msg_obj.get("detail", ""),
            )
        elif event_type == "error":
            self._chat_render_handler.render_event_card(
                "error", chat_box,
                session_key=target_tab,
                error_msg=msg_obj.get("error_msg", msg_obj.get("content", "Unknown error")),
            )
        elif event_type == "thinking":
            self._chat_render_handler.render_event_card(
                "thinking", chat_box,
                session_key=target_tab,
                thought_text=msg_obj.get("thought_text", msg_obj.get("content", "")),
            )
        self._mc.scroll_chat_to_bottom()

    def switch_to_tab(self, session_key: str):
        """
        Switch the chat notebook to the tab matching session_key.
        No-op if no matching tab exists.
        """
        notebook = self._mc.notebook
        for page_idx in range(notebook.get_n_pages()):
            if self._mc._tab_sessions.get(page_idx) == session_key:
                notebook.set_current_page(page_idx)
                break

    # ── Internal ─────────────────────────────────────────────────────────────

    def _dispatch(self, fn: Callable):
        """Call fn on the GTK main thread. Direct call if GLib not available."""
        if self._GLib is not None:
            def _wrap():
                fn()
                return False  # remove idle source after one execution
            self._GLib.idle_add(_wrap)
        else:
            fn()
