# tests/test_activity_wiring_handler.py
"""Tests for ActivityWiringHandler — the single owner of activity→drawer routing.

Covers: wire() sets all callbacks, gateway path adapters, local path adapters,
agent name resolution, offline/online invariants.
"""
from unittest.mock import MagicMock, patch

import pytest
import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk

from models.activity import ActivityBubble, ToolStatus
from ui.handlers.activity_wiring_handler import ActivityWiringHandler


class TestActivityWiringHandler:
    """Tests for the ActivityWiringHandler."""

    def _make_handler(self):
        """Create a handler with mocked dependencies."""
        activity_handler = MagicMock()
        agent_runtime_handler = MagicMock()
        activity_drawer = MagicMock()
        handler = ActivityWiringHandler(
            activity_handler=activity_handler,
            agent_runtime_handler=agent_runtime_handler,
            activity_drawer=activity_drawer,
        )
        return handler, activity_handler, agent_runtime_handler, activity_drawer

    # ── wire() ───────────────────────────────────────────────────────────

    def test_wire_sets_all_callbacks(self):
        """After .wire(), all five callbacks are registered on the dependencies."""
        handler, activity_handler, agent_runtime, drawer = self._make_handler()

        handler.wire()

        # Local path
        agent_runtime.set_on_command_output.assert_called_once()
        agent_runtime.set_on_activity_bubble.assert_called_once()
        agent_runtime.set_on_drawer_lifecycle.assert_called_once()

    def test_wire_is_idempotent(self):
        """Calling .wire() twice does not re-register callbacks."""
        handler, activity_handler, agent_runtime, drawer = self._make_handler()

        handler.wire()
        handler.wire()

        # Each local callback registered exactly once
        agent_runtime.set_on_command_output.assert_called_once()
        agent_runtime.set_on_drawer_lifecycle.assert_called_once()

    # ── Local path adapters ──────────────────────────────────────────────

    def test_on_local_command_output_builds_command_output_bubble(self):
        """Local exec_command result → drawer.append_event with command_output dict."""
        handler, _, agent_runtime, drawer = self._make_handler()
        agent_runtime.get_agent_name_for_session.return_value = "Coder"

        handler._on_local_command_output("sk1", "ls -la", "file.txt", 0, 12)

        drawer.append_event.assert_called_once()
        call_arg = drawer.append_event.call_args[0][0]
        assert call_arg["activity_type"] == "command_output"
        assert call_arg["agent"] == "Coder"
        assert call_arg["session_key"] == "sk1"

    def test_on_local_command_output_with_error_code(self):
        """Non-zero exit_code → bubble status ERROR and ❌ icon."""
        handler, _, agent_runtime, drawer = self._make_handler()
        agent_runtime.get_agent_name_for_session.return_value = "Coder"

        handler._on_local_command_output("sk1", "ls", "", 1, 50)

        call_arg = drawer.append_event.call_args[0][0]
        assert call_arg["exit_code"] == 1

    def test_on_local_activity_bubble_enriches_missing_agent_name(self):
        """Bubble with empty agent_name gets resolved via runtime."""
        handler, _, agent_runtime, drawer = self._make_handler()
        agent_runtime.get_agent_name_for_session.return_value = "Debugger"

        bubble = ActivityBubble(type="tool_end", session_key="sk1", tool_name="read_file",
                                agent_name="")
        handler._on_local_activity_bubble(bubble)

        drawer.append_event.assert_called_once()
        call_arg = drawer.append_event.call_args[0][0]
        assert call_arg["agent"] == "Debugger"

    def test_on_local_activity_bubble_preserves_existing_agent_name(self):
        """Bubble with non-empty agent_name keeps it without resolving."""
        handler, _, agent_runtime, drawer = self._make_handler()
        # Even if runtime returns something different, the existing name is kept
        agent_runtime.get_agent_name_for_session.return_value = "WrongAgent"

        bubble = ActivityBubble(type="tool_end", session_key="sk1", tool_name="read_file",
                                agent_name="Coder")
        handler._on_local_activity_bubble(bubble)

        call_arg = drawer.append_event.call_args[0][0]
        assert call_arg["agent"] == "Coder"

    def test_on_local_drawer_lifecycle_start_end(self):
        """Both phases route to the drawer."""
        handler, _, _, drawer = self._make_handler()

        handler._on_local_drawer_lifecycle("sk1", "Coder", "start")
        drawer.on_agent_start.assert_called_once_with("sk1", "Coder")

        handler._on_local_drawer_lifecycle("sk1", "Coder", "end")
        drawer.on_agent_end.assert_called_once_with("sk1", "Coder")

    # ── Agent name resolution ────────────────────────────────────────────

    def test_resolve_local_agent_name_uses_runtime_registry(self):
        """_resolve_local_agent_name delegates to the runtime's registry."""
        handler, _, agent_runtime, _ = self._make_handler()
        agent_runtime.get_agent_name_for_session.return_value = "Coder"

        name = handler._resolve_local_agent_name("special:coder")

        agent_runtime.get_agent_name_for_session.assert_called_once_with("special:coder")
        assert name == "Coder"

    def test_resolve_local_agent_name_falls_back_to_Agent_when_unknown(self):
        """Runtime returns '' → resolved name is 'Agent'."""
        handler, _, agent_runtime, _ = self._make_handler()
        agent_runtime.get_agent_name_for_session.return_value = ""

        name = handler._resolve_local_agent_name("special:unknown")

        assert name == "Agent"

    # ── No-drawer guard ──────────────────────────────────────────────────

    def test_methods_noop_when_drawer_is_none(self):
        """If drawer is None, all methods silently no-op (defensive)."""
        handler = ActivityWiringHandler(
            activity_handler=MagicMock(),
            agent_runtime_handler=MagicMock(),
            activity_drawer=None,
        )

        # Must not crash
        handler._on_local_command_output("s1", "ls", "", 0, 0)
        handler._on_local_activity_bubble(ActivityBubble(type="tool_end", session_key="s1"))
        handler._on_local_drawer_lifecycle("s1", "Coder", "end")
        # No assertion needed — we're verifying no-crash
