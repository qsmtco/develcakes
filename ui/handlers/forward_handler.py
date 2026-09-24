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
composition unit. The behavior is preserved verbatim — same order, same
comment-free local variables, same popover + bubble-rendering path, same
GLib.timeout_add scroll deferral, same latent "self._on_forward_message
may be None at first call" edge case. See ARCHITECTURE.md §3.6 (window.py
is the composition root) and §8.6 (handlers do not import each other).
"""

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402  (gi.require_version must run first)


class ForwardHandler:
    """
    Manages the agent-to-agent message forwarding flow.

    Owns: the popover widget construction, target agent resolution, and
          forwarded bubble rendering.

    Thread safety: called only on the main thread (button click handler).
    The GLib.timeout_add call inside forward_to_agent is main-thread-only
    by construction (timeout_add with a 16ms delay on the default main
    loop context).

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

    def forward_to_agent(
        self,
        target_session_key: str,
        text: str,
        source_session_key: str | None,
        popover,
    ) -> None:
        """Route forwarded text to target agent and show it in their tab.

        Body preserved verbatim from window._forward_to_agent (the former
        owner of this logic, ui/window.py lines 728–784). Same order, same
        gateway vs. special routing, same tab-create-or-select behavior,
        same latent ``self._chat_render_handler._on_forward_message``
        access (None until ChatHandler.set_on_forward_message propagates).
        """
        popover.popdown()
        if not text:
            return
        # Resolve source name from either special agents or gateway
        source_name = None
        if (self._agent_runtime_handler is not None
                and source_session_key in self._agent_runtime_handler.get_special_agents()):
            source_name = self._agent_runtime_handler.get_special_agents()[source_session_key]
        if not source_name and self._gateway_handler and self._gateway_handler.agent_mgr:
            source_name = self._gateway_handler.agent_mgr.get_name(source_session_key)
        # Route message to special or gateway agent
        is_special = (
            self._agent_runtime_handler is not None
            and target_session_key in self._agent_runtime_handler.get_special_agents()
        )
        if is_special:
            target_name = self._agent_runtime_handler.get_special_agents()[target_session_key]
            self._agent_runtime_handler.send_to_special_agent(target_session_key, text)
        else:
            target_name = (
                self._gateway_handler.agent_mgr.get_name(target_session_key)
                if self._gateway_handler and self._gateway_handler.agent_mgr
                else "Agent"
            )
            # Local path only (SPEC-05 R1); receiver no-ops for
            # unregistered/remote keys.
            self._agent_runtime_handler.send_to_special_agent(target_session_key, text)

        # Check if target agent already has an open tab
        target_tab_exists = None
        for page_idx, sk in self._main_content._tab_sessions.items():
            if sk == target_session_key:
                target_tab_exists = page_idx
                break

        if target_tab_exists is None:
            target_tab_exists = self._main_content.create_chat_tab(target_session_key, target_name)
        else:
            self._main_content._chat_notebook.set_current_page(target_tab_exists)

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
                # Defer scroll to ensure GTK has laid out the new bubble first
                GLib.timeout_add(16, lambda: (self._main_content.scroll_chat_to_bottom(target_tab_exists), False)[1])
