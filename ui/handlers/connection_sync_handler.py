"""
ui/handlers/connection_sync_handler.py

Owns the post-connect wiring of live references into all dependent handlers.

After the WebSocket handshake to the OpenClaw gateway completes, the
GatewayHandler fires a one-shot sync callback. This handler is the single
owner of that callback — it injects the live GatewayClient and AgentManager
into every handler that needs them, plus the cross-handler callbacks that
depend on the gateway being connected.

The extraction moves the 73-line body of the former
``window._sync_gateway_to_chat_handler`` (ui/window.py lines 613–685) into
its own composition unit. The behavior is preserved verbatim — same order,
same comments, same try/except around the optional ``utils.agent_defs``
import. See ARCHITECTURE.md §3.6 (window.py is the composition root).
"""

from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from gateway.client import GatewayClient


class ConnectionSyncHandler:
    """
    Coordinates the post-connect wiring of live references into all dependent handlers.

    Called once by GatewayHandler via set_sync_callback() after on_connected() dispatches.
    All other handlers are constructed with None/stub references at composition time;
    this handler injects the live GatewayClient and AgentManager after the WebSocket
    handshake completes.

    Thread safety: called on the gateway's background thread via GLib.idle_add() in
    GatewayHandler.on_connected(). All handler setters must be main-thread safe.

    Args:
        chat_handler:           ChatHandler instance — receives GatewayClient
        main_content:           MainContent instance — receives AgentManager
        agent_list_handler:     AgentListHandler instance — receives AgentManager
        gateway_handler:        GatewayHandler instance — source of AgentManager
        project_handler:        ProjectHandler instance — receives AgentManager + ReviewHandler
        command_handler:        CommandHandler instance — receives GatewayClient + AgentManager
        agent_command_handler:  AgentCommandHandler instance — receives many live refs
        session_handler:        SessionHandler instance — receives AgentManager
        feed_handler:           FeedHandler instance — receives audit report callback
        left_panel:             LeftPanel instance — receives refresh trigger
        review_handler:         ReviewHandler instance — wired into ProjectHandler
        activity_handler:       ActivityHandler instance — receives lifecycle / bubble callbacks
        agent_to_project:       dict-like — session-key → project mapping for routing
        on_forward_clicked:     Callable[[str, object, str | None], None] — forwarded-message
                                 entry point; window passes the ForwardHandler's
                                 show_forward_popover method.
        project_path_provider:  Callable[[], str | None] — for project path lookup
        main_window:            MainWindow instance — for update_agent_id_display (A-9)
    """

    def __init__(
        self,
        *,
        chat_handler,
        main_content,
        agent_list_handler,
        gateway_handler,
        project_handler,
        command_handler,
        agent_command_handler,
        session_handler,
        feed_handler,
        left_panel,
        review_handler,
        activity_handler,
        agent_to_project,
        on_forward_clicked: Callable,
        project_path_provider: Callable[[], str | None],
        main_window=None,
    ) -> None:
        self._chat_handler = chat_handler
        self._main_content = main_content
        self._agent_list_handler = agent_list_handler
        self._gateway_handler = gateway_handler
        self._project_handler = project_handler
        self._command_handler = command_handler
        self._agent_command_handler = agent_command_handler
        self._session_handler = session_handler
        self._feed_handler = feed_handler
        self._left_panel = left_panel
        self._review_handler = review_handler
        self._activity_handler = activity_handler
        self._agent_to_project = agent_to_project
        self._on_forward_clicked = on_forward_clicked
        self._project_path_provider = project_path_provider
        self._main_window = main_window

    def sync(self, gw: "GatewayClient") -> None:
        """
        Inject the live GatewayClient and AgentManager into all dependent handlers.
        Called once after gateway connect succeeds.

        Body preserved verbatim from window._sync_gateway_to_chat_handler (the former
        owner of this logic, ui/window.py lines 613–685). Same order, same comments,
        same bare-except around the optional agent_defs import.
        """
        self._main_content.set_agent_manager(self._gateway_handler.agent_mgr)
        # Wire AgentListHandler to the live AgentManager
        self._agent_list_handler.set_agent_mgr(self._gateway_handler.agent_mgr)
        # A-9: Update status bar agent ID label from gateway identity
        if self._main_window is not None:
            identity = gw.get_identity()
            agent_id = identity.get("device_id", "")
            if agent_id:
                self._main_window.update_agent_id_display(agent_id)
        # Refresh agents list for the currently open project — fixes members not
        # appearing after gateway reconnect (session keys change on reconnect)
        if self._project_handler.get_active_project_name():
            self._left_panel.refresh_agents_with_project(
                self._project_handler.get_active_project_name()
            )
        # Wire CommandHandler with live references after connect
        self._command_handler.set_agent_manager(self._gateway_handler.agent_mgr)
        # Wire ProjectHandler with live AgentManager for session lookup
        self._project_handler.set_agent_manager(self._gateway_handler.agent_mgr)
        # Wire ProjectHandler → ReviewHandler for cmd_status review state queries
        self._project_handler.set_review_handler(self._review_handler)
        # Wire AgentCommandHandler with live references after connect
        self._agent_command_handler.set_agent_manager(self._gateway_handler.agent_mgr)
        self._agent_command_handler.set_agent_routing(self._agent_to_project)
        self._agent_command_handler.set_project_handler(self._project_handler)
        self._agent_command_handler.set_project_path_provider(
            lambda: self._project_handler.get_active_project_path()
            if self._project_handler else None
        )
        try:
            from utils.agent_defs import load_agent_defs
            self._agent_command_handler.set_agent_defs_loader(load_agent_defs)
        except Exception:
            pass  # agent_defs optional at startup
        # Wire audit report feed card emission
        self._agent_command_handler.set_on_audit_report(
            lambda report: self._feed_handler.add_audit_report_card(
                report, project_name=self._project_handler.get_active_project_name() if self._project_handler else None
            )
        )
        # Wire SessionHandler with live AgentManager for session lookups
        self._session_handler.set_agent_manager(self._gateway_handler.agent_mgr)
        # Wire ChatHandler with AgentManager for display name resolution
        self._chat_handler.set_agent_manager(self._gateway_handler.agent_mgr)
        # PHASE 6: Inject AgentManager into ActivityHandler for agent_name
        # fallback (SPEC-activity-drawer §2.4). Placed before the activity
        # wiring block below so AgentManager is available by the time
        # activity events arrive.
        self._activity_handler.set_agent_manager(self._gateway_handler.agent_mgr)
        # Wire forward button callback
        self._chat_handler.set_on_forward_message(self._on_forward_clicked)
        # Wire send-initiated → ActivityHandler pre-flight state
        self._chat_handler.set_on_send_initiated(self._activity_handler.on_send_initiated)
        # Wire res confirmation → ActivityHandler pre-flight end
        self._chat_handler.set_on_res_confirmed(self._activity_handler.on_res_confirmed)
        # Wire lifecycle completed → ChatHandler fallback render (missing message bug fix)
        # Architecture: ActivityHandler tracks state; ChatHandler makes render decisions.
        self._activity_handler.set_on_lifecycle_completed(
            self._chat_handler._handle_lifecycle_completed
        )
        # Wire assistant text buffer so ChatHandler can populate its own buffer.
        # ChatHandler needs its own buffer for the recovery path when lifecycle
        # ends before any chat final arrives.
        self._activity_handler.set_on_assistant_buffer(self._chat_handler._buffer_assistant_text)
        # Wire agent start → clear render guard so subsequent responses render
        self._activity_handler.set_on_agent_start(self._chat_handler._clear_render_guard)
        # NOTE: ActivityBubble → drawer wiring has moved to ActivityWiringHandler.wire()
        # (called unconditionally at startup in window.py._build()). The three adapter
        # closures that formerly lived here (_bubble_to_row, _on_lifecycle, _on_command_output)
        # are now in ui/handlers/activity_wiring_handler.py.
        # This separation means the drawer works OFFLINE — no gateway needed.
        # The gateway path still goes through ActivityHandler.set_on_activity_bubble/.
        # set_on_agent_lifecycle are now set from ActivityWiringHandler.wire() at startup.
