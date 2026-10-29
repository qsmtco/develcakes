# tests/test_activity_bubbles.py
# Tests for Phase 2 of SPEC-smarter-chat-ux — Activity Bubbles.
#
# SPEC-58 §2i: class TestSystemBubbleCSS deleted with its subject —
# build_role_bubble died with ui/views/event_cards.py; the lifecycle-
# fallback case died with ChatHandler._handle_lifecycle_completed (the
# whole missing-message cluster is deleted). The surviving tests pin
# models/activity.py format_text and ActivityHandler._is_ui_active,
# which are live.

import gi
gi.require_version('Gtk', '4.0')

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
    """ChatHandler / ActivityHandler integration tests (Phase 2 SPEC-smarter-chat-ux).

    SPEC-58 §2i: the 4 _render_activity_bubble tests were REMOVED in
    SPEC-activity-drawer Phase 1 (those methods are deleted; activity flows
    to the ActivityPillAdapter, not chat), and the lifecycle-fallback case
    died with ChatHandler._handle_lifecycle_completed — the whole
    missing-message cluster is deleted (see the deleted
    tests/test_missing_message_fix.py).
    """

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

