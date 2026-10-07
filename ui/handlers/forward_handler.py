"""
ui/handlers/forward_handler.py

Owns the agent-to-agent message forwarding flow.

When a user clicks the forward button on a chat bubble, a popover is shown
listing every other agent the user could forward the message to. When the
user picks a target, the text is routed to that agent (special-agent or
gateway-agent), the target's chat tab is created or selected, and a new
"forwarded from <source>" bubble is rendered into it.

The extraction moves the bodies of the former ``window._on_forward_clicked``
and ``window._forward_to_agent`` (ui/window.py lines 684–784) into their own
composition unit. The extraction preserved the popover + bubble-rendering
path, the comment-free local variables, and the latent
"self._on_forward_message may be None at first call" edge case.

SPEC-12 SP6 DELIBERATE DEVIATION (BUG#28): the tab creation/selection now
precedes the send (the reply needs a live box), and both send sites pass
``reply_target=target_session_key`` (a private, agent-keyed reply). This is NOT
verbatim — the former order sent before creating the tab. See ARCHITECTURE.md §3.6 (window.py
is the composition root) and §8.6 (handlers do not import each other).
"""

import logging

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402  (gi.require_version must run first)

_logger = logging.getLogger(__name__)


class ForwardHandler:
    """
    Manages the agent-to-agent message forwarding flow.

    Owns: the popover widget construction, target agent resolution, and
          forwarded bubble rendering.

    Thread safety: called only on the main thread (button click handler).

    Args:
        main_content:           MainContent — for create_chat_tab, get_chat_box,
                                 _chat_notebook.set_current_page,
                                 scroll_chat_to_bottom, _tab_sessions
        chat_handler:           ChatHandler — placeholder for future evolution;
                                 not currently read by either method but kept
                                 on the constructor so the wiring in window.py
                                 can stay symmetric with ConnectionSyncHandler.
        chat_render_handler:    ChatRenderHandler — for render_sync (forwarded
                                 bubble) and _on_forward_message (the latent
                                 "may be None on first call" edge case)
        agent_runtime_handler:  AgentRuntimeHandler — for get_special_agents()
                                 and send_to_special_agent()
        gateway_handler:        GatewayHandler — for agent_mgr.get_name()
                                 (SPEC-05: send path is local-only now)
    """

    def __init__(
        self,
        *,
        main_content,
        chat_handler,
        chat_render_handler,
        agent_runtime_handler,
        gateway_handler,
    ) -> None:
        self._main_content = main_content
        self._chat_handler = chat_handler
        self._chat_render_handler = chat_render_handler
        self._agent_runtime_handler = agent_runtime_handler
        self._gateway_handler = gateway_handler

    def show_forward_popover(
        self,
        text: str,
        anchor_widget,
        source_session_key: str | None,
    ) -> None:
        """Build and display the forward-to-agent popover.

        Body preserved verbatim from window._on_forward_clicked (the former
        owner of this logic, ui/window.py lines 684–727). Same order, same
        Gtk widget construction, same default-arg capture pattern in the
        button-click lambdas.
        """
        # Build list of available agents:
        #   - Special agents (always available, even offline)
        #   - Gateway agents (only when connected)
        other_sessions = []

        if self._agent_runtime_handler is not None:
            for sk, name in self._agent_runtime_handler.get_special_agents().items():
                if source_session_key is None or sk != source_session_key:
                    other_sessions.append((sk, name))

        agent_mgr = self._gateway_handler.agent_mgr if self._gateway_handler else None
        if agent_mgr is not None:
            for page_idx, sk in self._main_content._tab_sessions.items():
                name = agent_mgr.get_name(sk)
                if name and (source_session_key is None or sk != source_session_key):
                    if not any(s == sk for s, _ in other_sessions):
                        other_sessions.append((sk, name))


        if not other_sessions:
            return  # nobody to forward to — silently skip

        popover = Gtk.Popover()
        popover.set_parent(anchor_widget)
        popover.set_position(Gtk.PositionType.TOP)

        menu_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        menu_box.set_margin_start(8)
        menu_box.set_margin_end(8)
        menu_box.set_margin_top(4)
        menu_box.set_margin_bottom(4)

        for sk, name in other_sessions:
            btn = Gtk.Button(label=f"→ {name}")
            btn.add_css_class("flat")
            btn.set_has_frame(False)
            btn.connect("clicked", lambda _b, s=sk, t=text, ss=source_session_key, pop=popover: self.forward_to_agent(s, t, ss, pop))
            menu_box.append(btn)

        popover.set_child(menu_box)
        popover.popup()

    def _ensure_target_tab(self, session_key, name, existing_page):
        """SPEC-12 BUG#28: create the private tab if absent (else select the
        existing one); return its page index. Shared by both routing branches
        so the create-or-select logic exists exactly once."""
        if existing_page is None:
            existing_page = self._main_content.create_chat_tab(session_key, name)
        else:
            self._main_content._chat_notebook.set_current_page(existing_page)
        return existing_page

    def forward_to_agent(
        self,
        target_session_key: str,
        text: str,
        source_session_key: str | None,
        popover,
    ) -> None:
        """Route forwarded text to target agent and show it in their tab.

        Body extracted from window._forward_to_agent (the former owner of this
        logic, ui/window.py lines 728–784): same gateway vs. special routing,
        same latent ``self._chat_render_handler._on_forward_message`` access
        (None until ChatHandler.set_on_forward_message propagates).

        SPEC-12 SP6 DEVIATION (BUG#28): the tab create/select now precedes the
        send, and both sends pass ``reply_target=target_session_key``.
        """
        popover.popdown()
        if not text:
            return
        # FIX 11 (SPEC-05 SP2 micro-round): forward_to_agent calls
        # send_to_special_agent unconditionally in the non-special else
        # branch — pre-SP2 an early-return guarded a None ARH. Mirror the
        # ChatHandler._send_local contract: drop the send with a WARNING
        # instead of raising AttributeError inside a GTK callback.
        if self._agent_runtime_handler is None:
            _logger.warning(
                "[forward] Forward dropped for %r - AgentRuntimeHandler "
                "not wired yet",
                target_session_key,
            )
            return
        # Resolve source name from either special agents or gateway
        source_name = None
        if (self._agent_runtime_handler is not None
                and source_session_key in self._agent_runtime_handler.get_special_agents()):
            source_name = self._agent_runtime_handler.get_special_agents()[source_session_key]
        if not source_name and self._gateway_handler and self._gateway_handler.agent_mgr:
            source_name = self._gateway_handler.agent_mgr.get_name(source_session_key)
        # SPEC-12 BUG#28: scan for the target's open tab FIRST — the tab is
        # created/selected BEFORE the send below, so the reply (produced by
        # the send) has a live box to render into.
        target_tab_exists = None
        for page_idx, sk in self._main_content._tab_sessions.items():
            if sk == target_session_key:
                target_tab_exists = page_idx
                break

        # Route message to special or gateway agent
        is_special = (
            self._agent_runtime_handler is not None
            and target_session_key in self._agent_runtime_handler.get_special_agents()
        )
        if is_special:
            target_name = self._agent_runtime_handler.get_special_agents()[target_session_key]
            target_tab_exists = self._ensure_target_tab(
                target_session_key, target_name, target_tab_exists)
            # SPEC-12 §2f: the forwarded message is a PRIVATE (agent-keyed)
            # send — its reply renders in the target's own tab.
            self._agent_runtime_handler.send_to_special_agent(
                target_session_key, text, reply_target=target_session_key)
        else:
            target_name = (
                self._gateway_handler.agent_mgr.get_name(target_session_key)
                if self._gateway_handler and self._gateway_handler.agent_mgr
                else "Agent"
            )
            target_tab_exists = self._ensure_target_tab(
                target_session_key, target_name, target_tab_exists)
            # Local path only (SPEC-05 R1); receiver no-ops for
            # unregistered/remote keys. Reply target = the target's own tab
            # (same private-view rule as the is_special branch).
            self._agent_runtime_handler.send_to_special_agent(
                target_session_key, text, reply_target=target_session_key)

        # Append forwarded bubble to the target tab
        chat_box = self._main_content.get_chat_box(target_tab_exists)
        if chat_box is not None and self._chat_render_handler is not None:
            bubble = self._chat_render_handler.render_sync(
                "You", text, target_session_key,
                on_forward_click=self._chat_render_handler._on_forward_message,
                forwarded_from=source_name,
                agent_name="You",
            )
            if bubble is not None:
                chat_box.append(bubble)
                # MICRO-SMART-SCROLL (BUG#1 scope-miss fix, 2026-10-07): the
                # forced `GLib.timeout_add(16, scroll_chat_to_bottom)` was
                # REMOVED — same class as the 18 calls dropped from the chat
                # handlers. The surface owns its own scroll (single-scroll
                # ruling): the WebKit surface self-heals via smart-scroll and
                # the TextViewFallback follows/restores itself. A forced
                # scroll here raced both, yanking a scrolled-up reader to the
                # bottom ~16ms after a forward.
