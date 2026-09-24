# tests/test_connection_sync_handler.py
# Tests for ui/handlers/connection_sync_handler.py — Phase 3a-1 extraction.
#
# What this tests:
#   ConnectionSyncHandler.sync(gw) wires live GatewayClient and AgentManager
#   into all dependent handlers after the gateway WebSocket handshake completes.
#   This test file verifies the wiring is correct (right targets, right args,
#   right order) without depending on real GTK, real gateways, or real
#   handler implementations.
#
# Principle: mock at the boundary, test behavior not internals.
# All dependencies are MagicMock instances. We assert on the public setter
# side effects (which handlers were called with which args) rather than on
# internal state. See ARCHITECTURE.md §8.6 (handler pattern: receive deps via
# setters, never import from ui/handlers/).

import pytest
from unittest.mock import MagicMock

from ui.handlers.connection_sync_handler import ConnectionSyncHandler


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def gw():
    """A MagicMock GatewayClient — represents the live client after connect."""
    return MagicMock(name="GatewayClient")


@pytest.fixture
def deps():
    """A bundle of MagicMock instances for all 16 ConnectionSyncHandler deps.

    Returned as a dict so individual tests can pick the dependencies they care
    about. All setters on the mock targets are no-ops by default; this lets
    the handler's sync() body run without raising, and lets tests assert
    exactly which setters were called with which args.
    """
    agent_mgr = MagicMock(name="AgentManager")
    agent_to_project = MagicMock(name="AgentRoutingTable")
    review_handler = MagicMock(name="ReviewHandler")

    # ChatHandler is a "wider" target — multiple setters, plus internal
    # attributes that sync() reads (e.g. chat_handler._handle_lifecycle_completed).
    # Set these as real sentinels so equality assertions are readable.
    # (SPEC-05 SP2: _awareness_sent sentinel deleted with the awareness machinery.)
    chat_handler = MagicMock(name="ChatHandler")
    chat_handler._handle_lifecycle_completed = MagicMock(name="lifecycle_completed")
    chat_handler._buffer_assistant_text = MagicMock(name="buffer_assistant_text")
    chat_handler._clear_render_guard = MagicMock(name="clear_render_guard")
    chat_handler._render_activity_bubble = MagicMock(name="render_activity_bubble")

    # ActivityHandler is the second "wider" target — sync() reads
    # activity_handler.on_send_initiated and on_res_confirmed. Provide real
    # sentinel callables so equality assertions don't compare MagicMocks.
    activity_handler = MagicMock(name="ActivityHandler")
    activity_handler.on_send_initiated = MagicMock(name="on_send_initiated")
    activity_handler.on_res_confirmed = MagicMock(name="on_res_confirmed")

    # GatewayHandler — the source of agent_mgr. sync() reaches through
    # gateway_handler.agent_mgr, so that attribute must be a real sentinel.
    gateway_handler = MagicMock(name="GatewayHandler")
    gateway_handler.agent_mgr = agent_mgr

    # ProjectHandler — sync() calls get_active_project_name() (gates the
    # left_panel.refresh call) and get_active_project_path() (inside the
    # lambda passed to set_project_path_provider).
    project_handler = MagicMock(name="ProjectHandler")
    project_handler.get_active_project_name.return_value = "test-project"
    project_handler.get_active_project_path.return_value = "/tmp/test-project"

    return {
        "chat_handler": chat_handler,
        "main_content": MagicMock(name="MainContent"),
        "agent_list_handler": MagicMock(name="AgentListHandler"),
        "gateway_handler": gateway_handler,
        "project_handler": project_handler,
        "command_handler": MagicMock(name="CommandHandler"),
        "agent_command_handler": MagicMock(name="AgentCommandHandler"),
        "session_handler": MagicMock(name="SessionHandler"),
        "feed_handler": MagicMock(name="FeedHandler"),
        "left_panel": MagicMock(name="LeftPanel"),
        "review_handler": review_handler,
        "activity_handler": activity_handler,
        "agent_to_project": agent_to_project,
        "on_forward_clicked": MagicMock(name="on_forward_clicked"),
        "project_path_provider": lambda: "/tmp/test-project",
        # Extras for assertions:
        "agent_mgr": agent_mgr,
    }


@pytest.fixture
def handler(deps):
    """A ConnectionSyncHandler wired with all 16 mock dependencies."""
    return ConnectionSyncHandler(
        chat_handler=deps["chat_handler"],
        main_content=deps["main_content"],
        agent_list_handler=deps["agent_list_handler"],
        gateway_handler=deps["gateway_handler"],
        project_handler=deps["project_handler"],
        command_handler=deps["command_handler"],
        agent_command_handler=deps["agent_command_handler"],
        session_handler=deps["session_handler"],
        feed_handler=deps["feed_handler"],
        left_panel=deps["left_panel"],
        review_handler=deps["review_handler"],
        activity_handler=deps["activity_handler"],
        agent_to_project=deps["agent_to_project"],
        on_forward_clicked=deps["on_forward_clicked"],
        project_path_provider=deps["project_path_provider"],
    )


# ── Tests ────────────────────────────────────────────────────────────────────


class TestChatHandlerWiring:
    """sync() injects the live GatewayClient and AgentManager into ChatHandler."""

    def test_sync_calls_chat_handler_set_agent_manager_once(
        self, handler, deps, gw
    ):
        """chat_handler.set_agent_manager is called once with the live agent_mgr
        (line 142 of handler body). It is NOT called twice — only the broader
        set_agent_manager setter pattern across all handlers totals 6 calls."""
        handler.sync(gw)
        assert deps["chat_handler"].set_agent_manager.call_count == 1
        deps["chat_handler"].set_agent_manager.assert_called_once_with(
            deps["agent_mgr"]
        )

    def test_sync_calls_chat_handler_set_on_forward_message_with_callback(
        self, handler, deps, gw
    ):
        handler.sync(gw)
        deps["chat_handler"].set_on_forward_message.assert_called_once_with(
            deps["on_forward_clicked"]
        )

    def test_sync_calls_chat_handler_set_on_send_initiated_with_activity_callback(
        self, handler, deps, gw
    ):
        handler.sync(gw)
        deps["chat_handler"].set_on_send_initiated.assert_called_once_with(
            deps["activity_handler"].on_send_initiated
        )

    def test_sync_calls_chat_handler_set_on_res_confirmed_with_activity_callback(
        self, handler, deps, gw
    ):
        handler.sync(gw)
        deps["chat_handler"].set_on_res_confirmed.assert_called_once_with(
            deps["activity_handler"].on_res_confirmed
        )


class TestMainContentAndAgentListWiring:
    """sync() wires AgentManager into MainContent and AgentListHandler."""

    def test_sync_calls_main_content_set_agent_manager(self, handler, deps, gw):
        handler.sync(gw)
        deps["main_content"].set_agent_manager.assert_called_once_with(
            deps["agent_mgr"]
        )

    def test_sync_calls_agent_list_handler_set_agent_mgr(self, handler, deps, gw):
        handler.sync(gw)
        deps["agent_list_handler"].set_agent_mgr.assert_called_once_with(
            deps["agent_mgr"]
        )


class TestCommandHandlerWiring:
    """sync() wires GatewayClient and AgentManager into CommandHandler."""

    def test_sync_calls_command_handler_set_agent_manager(self, handler, deps, gw):
        handler.sync(gw)
        deps["command_handler"].set_agent_manager.assert_called_once_with(
            deps["agent_mgr"]
        )


class TestProjectHandlerWiring:
    """sync() wires AgentManager and ReviewHandler into ProjectHandler."""

    def test_sync_calls_project_handler_set_agent_manager(self, handler, deps, gw):
        handler.sync(gw)
        deps["project_handler"].set_agent_manager.assert_called_once_with(
            deps["agent_mgr"]
        )

    def test_sync_calls_project_handler_set_review_handler_with_self_review(
        self, handler, deps, gw
    ):
        handler.sync(gw)
        deps["project_handler"].set_review_handler.assert_called_once_with(
            deps["review_handler"]
        )


class TestAgentCommandHandlerWiring:
    """sync() wires AgentManager/routing/project refs into AgentCommandHandler.

    (SPEC-05 SP2: the set_gateway_client and set_awareness_sent wiring tests
    were deleted with the setters themselves — both died with the R1 strip.)"""

    def test_sync_calls_agent_command_handler_set_agent_manager(
        self, handler, deps, gw
    ):
        handler.sync(gw)
        deps["agent_command_handler"].set_agent_manager.assert_called_once_with(
            deps["agent_mgr"]
        )

    def test_sync_calls_agent_command_handler_set_agent_routing_with_table(
        self, handler, deps, gw
    ):
        handler.sync(gw)
        deps["agent_command_handler"].set_agent_routing.assert_called_once_with(
            deps["agent_to_project"]
        )

    def test_sync_calls_agent_command_handler_set_project_handler(
        self, handler, deps, gw
    ):
        handler.sync(gw)
        deps["agent_command_handler"].set_project_handler.assert_called_once_with(
            deps["project_handler"]
        )

    def test_sync_calls_agent_command_handler_set_project_path_provider_with_lambda(
        self, handler, deps, gw
    ):
        handler.sync(gw)
        # The exact lambda shape is preserved: returns project_handler's
        # active path if project_handler is truthy, else None.
        deps["agent_command_handler"].set_project_path_provider.assert_called_once()
        # Capture the lambda and invoke it to verify behavior
        passed_lambda = deps[
            "agent_command_handler"
        ].set_project_path_provider.call_args.args[0]
        assert callable(passed_lambda)
        assert passed_lambda() == "/tmp/test-project"


class TestAgentDefsLoaderImport:
    """The try/except around `from utils.agent_defs import load_agent_defs` is
    intentional — agent_defs is optional at startup. Verify both branches."""

    def test_sync_calls_set_agent_defs_loader_when_import_succeeds(
        self, handler, deps, gw, monkeypatch
    ):
        """If utils.agent_defs imports cleanly (current state), the loader
        IS passed to set_agent_defs_loader."""
        # utils.agent_defs is already importable in the test env (verified
        # in Phase 3a-1). We just confirm set_agent_defs_loader is called.
        handler.sync(gw)
        deps["agent_command_handler"].set_agent_defs_loader.assert_called_once()

    def test_sync_silently_skips_set_agent_defs_loader_when_import_fails(
        self, handler, deps, gw, monkeypatch
    ):
        """If the import raises, sync() must not raise, and set_agent_defs_loader
        must NOT be called. This is the bare-except preservation rule."""
        # Force the import inside sync() to fail by patching the module so
        # the import statement raises ImportError.
        import sys
        # Simulate the import failure by removing the module from sys.modules
        # and inserting a stub that raises on attribute access. Cleanest:
        # patch utils.agent_defs to be a MagicMock that raises on the
        # specific attribute.
        saved = sys.modules.get("utils.agent_defs")
        fake_mod = MagicMock()
        # Make the `from utils.agent_defs import load_agent_defs` succeed at
        # first (so the module loads), but raise on accessing load_agent_defs.
        # Actually, `from X import Y` first imports X, then accesses Y. So
        # we want X import to fail. We do that by setting X to None in
        # sys.modules, which makes `from X import Y` raise ImportError.
        sys.modules["utils.agent_defs"] = None  # forces ImportError on import
        try:
            handler.sync(gw)  # must NOT raise
        finally:
            # Restore
            if saved is not None:
                sys.modules["utils.agent_defs"] = saved
            else:
                sys.modules.pop("utils.agent_defs", None)

        deps["agent_command_handler"].set_agent_defs_loader.assert_not_called()


class TestAuditReportWiring:
    """sync() wires the audit-report → feed-card callback into AgentCommandHandler."""

    def test_sync_calls_set_on_audit_report_with_lambda_calling_feed_handler(
        self, handler, deps, gw
    ):
        handler.sync(gw)
        deps["agent_command_handler"].set_on_audit_report.assert_called_once()
        # Invoke the lambda with a fake report and verify it calls
        # feed_handler.add_audit_report_card with the report + project name.
        passed_lambda = deps[
            "agent_command_handler"
        ].set_on_audit_report.call_args.args[0]
        fake_report = {"summary": "all good"}
        passed_lambda(fake_report)
        deps["feed_handler"].add_audit_report_card.assert_called_once_with(
            fake_report, project_name="test-project"
        )


class TestSessionAndRefreshWiring:
    """sync() wires AgentManager into SessionHandler and refreshes the left
    panel if a project is currently open."""

    def test_sync_calls_session_handler_set_agent_manager(self, handler, deps, gw):
        handler.sync(gw)
        deps["session_handler"].set_agent_manager.assert_called_once_with(
            deps["agent_mgr"]
        )

    def test_sync_calls_left_panel_refresh_when_project_active(
        self, handler, deps, gw
    ):
        """If project_handler.get_active_project_name() returns a name, sync()
        calls left_panel.refresh_agents_with_project with that name."""
        deps["project_handler"].get_active_project_name.return_value = "my-project"
        handler.sync(gw)
        deps["left_panel"].refresh_agents_with_project.assert_called_once_with(
            "my-project"
        )

    def test_sync_skips_left_panel_refresh_when_no_active_project(
        self, handler, deps, gw
    ):
        """If project_handler.get_active_project_name() returns None, sync()
        does NOT call left_panel.refresh_agents_with_project."""
        deps["project_handler"].get_active_project_name.return_value = None
        handler.sync(gw)
        deps["left_panel"].refresh_agents_with_project.assert_not_called()


class TestActivityHandlerWiring:
    """sync() wires 4 lifecycle callbacks from ChatHandler into ActivityHandler."""

    def test_sync_calls_activity_handler_set_on_lifecycle_completed(
        self, handler, deps, gw
    ):
        handler.sync(gw)
        deps[
            "activity_handler"
        ].set_on_lifecycle_completed.assert_called_once_with(
            deps["chat_handler"]._handle_lifecycle_completed
        )

    def test_sync_calls_activity_handler_set_on_assistant_buffer(
        self, handler, deps, gw
    ):
        handler.sync(gw)
        deps["activity_handler"].set_on_assistant_buffer.assert_called_once_with(
            deps["chat_handler"]._buffer_assistant_text
        )

    def test_sync_calls_activity_handler_set_on_agent_start(
        self, handler, deps, gw
    ):
        handler.sync(gw)
        deps["activity_handler"].set_on_agent_start.assert_called_once_with(
            deps["chat_handler"]._clear_render_guard
        )

    def test_sync_no_longer_wires_drawer_callbacks(
        self, handler, deps, gw
    ):
        # Drawer wiring has moved from sync() to ActivityWiringHandler.wire().
        # sync() should NOT call set_on_activity_bubble, set_on_agent_lifecycle,
        # or set_on_command_output. It should still call set_agent_manager.
        handler.sync(gw)
        deps["activity_handler"].set_on_activity_bubble.assert_not_called()
        deps["activity_handler"].set_on_agent_lifecycle.assert_not_called()
        # AgentRuntimeHandler.set_on_command_output should also not be called
        if deps.get("chat_handler", MagicMock())._agent_runtime_handler is not None:
            deps["chat_handler"]._agent_runtime_handler.set_on_command_output.assert_not_called()
        # But set_agent_manager IS still called
        deps["activity_handler"].set_agent_manager.assert_called_once()

    def test_sync_drawer_wiring_removed_completely(
        self, handler, deps, gw
    ):
        # Former SPEC-activity-drawer wiring inside sync() has been extracted
        # to ActivityWiringHandler. sync() no longer has any drawer-related code.
        from models.activity import ActivityBubble

        # No drawer is needed — sync() doesn't reference it
        handler.sync(gw)

        # Verify drawer-related callbacks are NOT set from sync()
        deps["activity_handler"].set_on_activity_bubble.assert_not_called()
        deps["activity_handler"].set_on_agent_lifecycle.assert_not_called()


class TestOrder:
    """sync() is documented to perform wiring in a specific order — verify the
    setters that depend on gateway_handler.agent_mgr being live (i.e. setters
    that read it) get the same value. This protects against a regression
    where someone reorders the body and accidentally reads agent_mgr before
    it's been populated."""

    def test_all_set_agent_manager_calls_receive_the_same_agent_mgr(
        self, handler, deps, gw
    ):
        handler.sync(gw)
        # set_agent_manager is called on: main_content, command_handler,
        # project_handler, agent_command_handler, session_handler, chat_handler.
        # Total = 6 calls (one per handler). All should receive deps["agent_mgr"].
        all_callers = [
            deps["main_content"],
            deps["command_handler"],
            deps["project_handler"],
            deps["agent_command_handler"],
            deps["session_handler"],
            deps["chat_handler"],
        ]
        for caller in all_callers:
            assert caller.set_agent_manager.call_count == 1, (
                f"{caller._mock_name or 'mock'} received set_agent_manager "
                f"{caller.set_agent_manager.call_count} times, expected 1"
            )
            for call in caller.set_agent_manager.call_args_list:
                assert call.args == (deps["agent_mgr"],), (
                    f"{caller._mock_name} got {call.args!r}, expected ({deps['agent_mgr']!r},)"
                )

        # Aggregate count: 6 distinct callers × 1 call each = 6
        total = sum(
            caller.set_agent_manager.call_count for caller in all_callers
        )
        assert total == 6


class TestSyncRealHandlerClasses:
    """FIX 2 pin (SP2 audit BUG #1): sync() must run to COMPLETION against REAL
    handler classes — not MagicMocks, which silently absorb AttributeError on
    deleted setters and masked the 3-call break for a full audit cycle.

    Completion is asserted structurally: every setter sync() is required to
    call post-SP2 has actually fired by the time sync() returns. The LAST
    statement in sync() is the agent-start → clear-render-guard wiring, so
    set_on_agent_start firing proves the whole body executed (Debugger's
    amended framing: not just no-raise, but ran-to-end)."""

    def test_sync_runs_to_completion_post_sp2(self):
        from unittest.mock import MagicMock

        from ui.handlers.agent_command_handler import AgentCommandHandler
        from ui.handlers.chat_handler import ChatHandler
        from ui.handlers.command_handler import CommandHandler

        chat_handler = ChatHandler(
            main_content=MagicMock(name="mc"),
            agent_to_project=MagicMock(name="routing"),
            projects_module=MagicMock(name="projects"),
            GLib_module=None,
        )
        command_handler = CommandHandler(
            agent_manager=None, project_handler=None, GLib_module=None
        )
        agent_command_handler = AgentCommandHandler(GLib_module=None)

        recorded: dict[str, object] = {}

        class GWHandler:
            """Minimal gateway handler stub — sync() reads .agent_mgr."""

            agent_mgr = MagicMock(name="AgentManager")

        class ProjectHandler:
            def get_active_project_name(self):
                return None  # skip left-panel refresh branch

            def get_active_project_path(self):
                return None

            def set_agent_manager(self, mgr):
                recorded["project_agent_mgr"] = mgr

            def set_review_handler(self, rh):
                recorded["project_review"] = rh

        class ActivityHandler:
            on_send_initiated = staticmethod(lambda sk: None)
            on_res_confirmed = staticmethod(lambda sk: None)

            def set_agent_manager(self, mgr):
                recorded["activity_agent_mgr"] = mgr

            def set_on_lifecycle_completed(self, cb):
                recorded["lifecycle"] = cb

            def set_on_assistant_buffer(self, cb):
                recorded["buffer"] = cb

            def set_on_agent_start(self, cb):
                recorded["agent_start"] = cb  # <- the completion sentinel

        class MainContent:
            def set_agent_manager(self, mgr):
                recorded["mc_agent_mgr"] = mgr

        class AgentListHandler:
            def set_agent_mgr(self, mgr):
                recorded["list_agent_mgr"] = mgr

        class SessionHandler:
            def set_agent_manager(self, mgr):
                recorded["session_agent_mgr"] = mgr

        class FeedHandler:
            def add_audit_report_card(self, report, project_name=None):
                pass

        class LeftPanel:
            def refresh_agents_with_project(self, name):
                recorded["refresh"] = name

        class GW:
            def get_identity(self):
                return {"device_id": "test-device"}

        handler = ConnectionSyncHandler(
            chat_handler=chat_handler,
            main_content=MainContent(),
            agent_list_handler=AgentListHandler(),
            gateway_handler=GWHandler(),
            project_handler=ProjectHandler(),
            command_handler=command_handler,
            agent_command_handler=agent_command_handler,
            session_handler=SessionHandler(),
            feed_handler=FeedHandler(),
            left_panel=LeftPanel(),
            review_handler=MagicMock(name="review"),
            activity_handler=ActivityHandler(),
            agent_to_project={},
            on_forward_clicked=lambda *a, **k: None,
            project_path_provider=lambda: None,
            main_window=None,
        )

        # Must not raise — and must reach the final wiring statement.
        handler.sync(GW())

        # Ran-to-completion proof: the LAST wiring call in sync() fired.
        assert "agent_start" in recorded, (
            "sync() did not run to completion — set_on_agent_start (its final "
            "statement) never fired"
        )
        assert recorded["agent_start"] == chat_handler._clear_render_guard
        # Spot-check the mid-body wiring on the real classes.
        assert recorded["project_review"] is not None
        assert chat_handler._agent_mgr is GWHandler.agent_mgr
