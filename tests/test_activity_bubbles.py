# tests/test_activity_bubbles.py
# Tests for Phase 2 of SPEC-smarter-chat-ux — Activity Bubbles.
#
# Architecture:
#   local AgentRuntimeHandler bubbles → ActivityDrawer
#   ChatHandler._render_activity_bubble(bubble) → calls _render_activity_bubble_impl on main thread
#   ChatRenderHandler.render_sync("System", text, ...) → build_role_bubble("System", text)
#   build_role_bubble assigns .chat-bubble-System CSS class for activity bubble styling

import gi
gi.require_version('Gtk', '4.0')

import time

import pytest
from unittest.mock import MagicMock


class TestActivityBubbleModel:
    """ActivityBubble dataclass — format_text() produces correct bubble text."""

    def test_lifecycle_start(self):
        from models.activity import ActivityBubble
        b = ActivityBubble(type="lifecycle_start", session_key="sk-1")
        assert b.format_text() == "thinking..."

    def test_tool_start(self):
        from models.activity import ActivityBubble
        b = ActivityBubble(type="tool_start", session_key="sk-1", tool_name="web_search")
        assert b.format_text() == "search"

    def test_tool_end(self):
        from models.activity import ActivityBubble
        b = ActivityBubble(type="tool_end", session_key="sk-1", tool_name="web_search", duration_ms=1247)
        assert b.format_text() == "search  1,247ms"

    def test_tool_error(self):
        from models.activity import ActivityBubble
        b = ActivityBubble(type="tool_error", session_key="sk-1", tool_name="read_file")
        assert b.format_text() == "read file  failed"

    def test_plan(self):
        from models.activity import ActivityBubble
        b = ActivityBubble(type="plan", session_key="sk-1", title="Refactor auth", steps=["Step 1", "Step 2", "Step 3"])
        text = b.format_text()
        assert "plan: Refactor auth" in text
        assert "3 steps" in text

    def test_approval_request(self):
        from models.activity import ActivityBubble
        b = ActivityBubble(type="approval_request", session_key="sk-1", command="rm -rf /")
        assert b.format_text() == "approve: rm -rf /"

    def test_command_output(self):
        from models.activity import ActivityBubble
        b = ActivityBubble(type="command_output", session_key="sk-1", tool_name="git diff", exit_code=0, duration_ms=4521)
        assert b.format_text() == "git diff  4,521ms"

    def test_patch(self):
        from models.activity import ActivityBubble
        b = ActivityBubble(type="patch", session_key="sk-1", tool_name="edit_file", added=3, modified=7, deleted=1)
        text = b.format_text()
        assert "+3" in text
        assert "~7" in text
        assert "-1" in text

    def test_patch_partial(self):
        from models.activity import ActivityBubble
        b = ActivityBubble(type="patch", session_key="sk-1", tool_name="edit_file", added=1, modified=0, deleted=0, icon="✏️")
        text = b.format_text()
        assert "+1" in text
        assert "~0" not in text
        assert "-0" not in text


class TestChatHandlerActivityBubbleRender:
    """ChatHandler integration tests for activity/lifecycle routing (Phase 2 SPEC-smarter-chat-ux).

    The 4 _render_activity_bubble tests were REMOVED in SPEC-activity-drawer Phase 1
    (those methods are deleted; activity now flows to ActivityDrawer, not chat).
    """

    def test_lifecycle_fallback_routes_to_project_tab(self, fake_glib):
        """Lifecycle fallback resolves to project tab when agent has no direct tab."""
        from ui.handlers.chat_handler import ChatHandler

        class FakeRouting:
            def get_project(self, sk):
                return "crabwatch" if sk == "agent:qaster" else None
        
        routing = FakeRouting()
        mock_project_chat_box = MagicMock()
        mock_mc = MagicMock()
        mock_mc.get_chat_box_for_session = lambda sk: (
            mock_project_chat_box if sk == "project:crabwatch" else None
        )
        mock_mc.get_current_session_key = MagicMock(return_value="project:crabwatch")

        handler = ChatHandler(
            main_content=mock_mc,
            agent_to_project=routing,
            projects_module=MagicMock(),
            GLib_module=fake_glib,
        )

        fake_render = MagicMock()
        fake_render.is_streaming.return_value = False
        fake_render.render_sync.return_value = MagicMock()
        handler._chat_render_handler = fake_render

        # Fire lifecycle fallback: agent key "agent:qaster" has project "crabwatch"
        handler._handle_lifecycle_completed("agent:qaster", "Response text from fallback")

        # Should have called _handle_final_response with project:crabwatch tab
        fake_render.render_sync.assert_called_once()
        mock_project_chat_box.append.assert_called_once()

    def test_is_ui_active_resolves_project_tab_for_agent(self, fake_glib):
        """_is_ui_active returns True when active tab is the project tab for the agent."""
        from ui.handlers.activity_handler import ActivityHandler

        class FakeRouting:
            def get_project(self, sk):
                return "crabwatch" if sk == "agent:qaster" else None
        
        routing = FakeRouting()
        mc = MagicMock()
        mc.get_current_session_key = MagicMock(return_value="project:crabwatch")

        ah = ActivityHandler(status_target=MagicMock(), main_content=mc, GLib_module=fake_glib)
        ah.set_agent_routing(routing)

        # Agent key belongs to active project tab → considered active
        assert ah._is_ui_active("agent:qaster") is True
        # Agent key has no project routing → not active
        assert ah._is_ui_active("agent:unknown") is False
        # Direct tab match still works
        assert ah._is_ui_active("project:crabwatch") is True
        # None is always active
        assert ah._is_ui_active(None) is True

    def test_is_ui_active_no_routing_table(self, fake_glib):
        """Without routing table, _is_ui_active falls back to direct key comparison."""
        from ui.handlers.activity_handler import ActivityHandler

        mc = MagicMock()
        mc.get_current_session_key = MagicMock(return_value="project:crabwatch")

        ah = ActivityHandler(status_target=MagicMock(), main_content=mc, GLib_module=fake_glib)
        # No routing table set
        ah._agent_to_project = None

        # Agent key → project tab (no routing table to resolve it)
        assert ah._is_ui_active("agent:qaster") is False
        # Direct match still works
        assert ah._is_ui_active("project:crabwatch") is True


class TestSystemBubbleCSS:
    """System bubbles get the .chat-bubble-System CSS class."""

    def _walk_css_classes(self, widget):
        """Walk all CSS classes from widget and its descendants (GTK4 compatible)."""
        classes = []
        if hasattr(widget, 'get_css_classes'):
            classes.extend(widget.get_css_classes())
        if hasattr(widget, 'get_first_child'):
            child = widget.get_first_child()
            while child is not None:
                classes.extend(self._walk_css_classes(child))
                sibling = child.get_next_sibling() if hasattr(child, 'get_next_sibling') else None
                child = sibling
        return classes

    def test_system_role_uses_system_css_class(self):
        from ui.views.event_cards import build_role_bubble
        widget = build_role_bubble("System", "test activity bubble")
        classes = self._walk_css_classes(widget)
        assert "chat-bubble-System" in classes

    def test_agent_role_uses_agent_css_class(self):
        from ui.views.event_cards import build_role_bubble
        widget = build_role_bubble("Agent", "hello")
        classes = self._walk_css_classes(widget)
        assert "chat-bubble-agent" in classes
        assert "chat-bubble-System" not in classes

    def test_you_role_uses_you_css_class(self):
        from ui.views.event_cards import build_role_bubble
        widget = build_role_bubble("You", "hello")
        classes = self._walk_css_classes(widget)
        assert "chat-bubble-you" in classes
        assert "chat-bubble-System" not in classes

