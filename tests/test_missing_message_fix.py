# tests/test_missing_message_fix.py
# ChatHandler fallback render when a lifecycle completion arrives after chat final.
# Gateway ingress tests were removed in SPEC-16 SP4.

import gi
gi.require_version('Gtk', '4.0')

import pytest
from unittest.mock import MagicMock


class TestChatHandlerBufferRecovery:
    """ChatHandler recovers from empty chat final using its own assistant text buffer."""

    def test_chat_handler_buffers_assistant_text(self, fake_glib):
        """ChatHandler._buffer_assistant_text populates its own buffer."""
        from ui.handlers.chat_handler import ChatHandler
        handler = ChatHandler(
            main_content=MagicMock(),
            agent_to_project=MagicMock(),
            projects_module=MagicMock(),
            GLib_module=fake_glib,
        )

        handler._buffer_assistant_text("sk-1", "Buffered response")

        assert handler._assistant_text_buffer.get("sk-1") == "Buffered response"

    def test_lifecycle_completed_callback_renders_fallback(self, fake_glib):
        """_handle_lifecycle_completed dispatches _handle_final_response with buffered text."""
        from ui.handlers.chat_handler import ChatHandler
        handler = ChatHandler(
            main_content=MagicMock(),
            agent_to_project=MagicMock(),
            projects_module=MagicMock(),
            GLib_module=fake_glib,
        )

        # Buffer assistant text
        handler._buffer_assistant_text("sk-1", "Fallback response")

        # Mock _dispatch to capture the lambda args
        dispatch_args = {}
        def capture_dispatch(fn):
            # Resolve the lambda by calling it — captures args in closure
            dispatch_args['fn'] = fn
        handler._dispatch = capture_dispatch

        handler._handle_lifecycle_completed("sk-1", "Fallback response")

        assert 'fn' in dispatch_args
        # Call the captured lambda to extract positional args
        # lambda t=target_tab, sk=session_key, txt=buffered_text: ...
        dispatch_args['fn']()  # should not raise

    def test_no_double_render_when_chat_final_already_rendered(self, fake_glib):
        """_chat_final_rendered guard prevents double-render."""
        from ui.handlers.chat_handler import ChatHandler
        handler = ChatHandler(
            main_content=MagicMock(),
            agent_to_project=MagicMock(),
            projects_module=MagicMock(),
            GLib_module=fake_glib,
        )

        # Simulate that chat final already rendered for this session
        handler._chat_final_rendered["sk-1"] = True

        dispatch_called = []
        handler._dispatch = lambda *args, **kwargs: dispatch_called.append((args, kwargs))

        handler._handle_lifecycle_completed("sk-1", "Late fallback text")

        # Guard should have blocked the render
        assert len(dispatch_called) == 0


class TestRenderGuardClearsOnNewRound:
    """_chat_final_rendered guard must be cleared when a new agent round starts."""

    def test_guard_cleared_on_agent_start(self, fake_glib):
        """_clear_render_guard removes the guard so next round can render."""
        from ui.handlers.chat_handler import ChatHandler
        handler = ChatHandler(
            main_content=MagicMock(),
            agent_to_project=MagicMock(),
            projects_module=MagicMock(),
            GLib_module=fake_glib,
        )
        # Set guard
        handler._chat_final_rendered["sk-1"] = True
        # Clear it
        handler._clear_render_guard("sk-1")
        assert "sk-1" not in handler._chat_final_rendered

