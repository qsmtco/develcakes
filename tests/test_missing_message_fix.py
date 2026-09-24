# tests/test_missing_message_fix.py
# Tests for the fallback render path when chat final has no message.
#
# Phase 1 of SPEC-smarter-chat-ux:
#   ActivityHandler: tracks state (_assistant_text_buffer, _lifecycle_ended).
#     set_on_assistant_buffer(cb) — forwards each assistant text to ChatHandler
#     set_on_lifecycle_completed(cb) — fires when phase=end or phase=error
#   ChatHandler: makes render decisions.
#     _buffer_assistant_text(sk, text) — populates its own buffer
#     _handle_lifecycle_completed(sk, text) — fallback render via callback
#     _chat_final_rendered guard — prevents double-render

import gi
gi.require_version('Gtk', '4.0')

import pytest
from unittest.mock import MagicMock


class TestActivityHandlerAssistantBuffer:
    """ActivityHandler buffers and forwards agent stream=assistant text."""

    def test_buffer_stores_last_text(self, fake_glib):
        """Multiple assistant events — buffer keeps the last one."""
        from ui.handlers.activity_handler import ActivityHandler
        handler = ActivityHandler(
            feedbar=MagicMock(), main_content=MagicMock(), GLib_module=fake_glib
        )

        handler.on_gateway_event("agent", {
            "stream": "assistant",
            "sessionKey": "agent:test:1",
            "runId": "run-1",
            "data": {"text": "Hello"}
        })
        handler.on_gateway_event("agent", {
            "stream": "assistant",
            "sessionKey": "agent:test:1",
            "runId": "run-1",
            "data": {"text": "Hello world, here is the full response."}
        })

        assert handler._assistant_text_buffer.get("agent:test:1") == "Hello world, here is the full response."

    def test_buffer_forwards_to_callback(self, fake_glib):
        """set_on_assistant_buffer callback is called with session key and text."""
        from ui.handlers.activity_handler import ActivityHandler
        handler = ActivityHandler(
            feedbar=MagicMock(), main_content=MagicMock(), GLib_module=fake_glib
        )
        cb = MagicMock()
        handler.set_on_assistant_buffer(cb)

        handler.on_gateway_event("agent", {
            "stream": "assistant", "sessionKey": "sk-1", "runId": "r1",
            "data": {"text": "Forwarded text"}
        })

        cb.assert_called_once_with("sk-1", "Forwarded text")

    def test_buffer_per_session(self, fake_glib):
        """Different sessions have independent buffers."""
        from ui.handlers.activity_handler import ActivityHandler
        handler = ActivityHandler(
            feedbar=MagicMock(), main_content=MagicMock(), GLib_module=fake_glib
        )

        handler.on_gateway_event("agent", {
            "stream": "assistant", "sessionKey": "sk-1", "runId": "r1",
            "data": {"text": "Response for session 1"}
        })
        handler.on_gateway_event("agent", {
            "stream": "assistant", "sessionKey": "sk-2", "runId": "r2",
            "data": {"text": "Response for session 2"}
        })

        assert handler._assistant_text_buffer["sk-1"] == "Response for session 1"
        assert handler._assistant_text_buffer["sk-2"] == "Response for session 2"

    def test_buffer_empty_text_ignored(self, fake_glib):
        """Empty text does not overwrite existing buffer."""
        from ui.handlers.activity_handler import ActivityHandler
        handler = ActivityHandler(
            feedbar=MagicMock(), main_content=MagicMock(), GLib_module=fake_glib
        )

        handler.on_gateway_event("agent", {
            "stream": "assistant", "sessionKey": "sk-1", "runId": "r1",
            "data": {"text": "Hello"}
        })
        handler.on_gateway_event("agent", {
            "stream": "assistant", "sessionKey": "sk-1", "runId": "r1",
            "data": {"text": ""}
        })

        assert handler._assistant_text_buffer.get("sk-1") == "Hello"


class TestLifecycleCompletedCallback:
    """set_on_lifecycle_completed fires when phase=end or phase=error."""

    def test_callback_fires_on_lifecycle_end(self, fake_glib):
        """lifecycle phase=end fires the lifecycle-completed callback."""
        from ui.handlers.activity_handler import ActivityHandler
        handler = ActivityHandler(
            feedbar=MagicMock(), main_content=MagicMock(), GLib_module=fake_glib
        )

        # Pre-buffer some assistant text
        handler.on_gateway_event("agent", {
            "stream": "assistant", "sessionKey": "sk-1", "runId": "run-1",
            "data": {"text": "Buffered response"}
        })

        cb = MagicMock()
        handler.set_on_lifecycle_completed(cb)

        handler.on_gateway_event("agent", {
            "stream": "lifecycle",
            "sessionKey": "sk-1",
            "runId": "run-1",
            "data": {"phase": "end"}
        })

        cb.assert_called_once_with("sk-1", "Buffered response")

    def test_callback_fires_on_lifecycle_error(self, fake_glib):
        """lifecycle phase=error also fires the lifecycle-completed callback."""
        from ui.handlers.activity_handler import ActivityHandler
        handler = ActivityHandler(
            feedbar=MagicMock(), main_content=MagicMock(), GLib_module=fake_glib
        )

        handler.on_gateway_event("agent", {
            "stream": "assistant", "sessionKey": "sk-err", "runId": "run-err",
            "data": {"text": "Error response"}
        })

        cb = MagicMock()
        handler.set_on_lifecycle_completed(cb)

        handler.on_gateway_event("agent", {
            "stream": "lifecycle",
            "sessionKey": "sk-err",
            "runId": "run-err",
            "data": {"phase": "error"}
        })

        cb.assert_called_once_with("sk-err", "Error response")

    def test_callback_empty_text_still_fires(self, fake_glib):
        """Callback fires even when there is no buffered text (empty response)."""
        from ui.handlers.activity_handler import ActivityHandler
        handler = ActivityHandler(
            feedbar=MagicMock(), main_content=MagicMock(), GLib_module=fake_glib
        )

        cb = MagicMock()
        handler.set_on_lifecycle_completed(cb)

        handler.on_gateway_event("agent", {
            "stream": "lifecycle",
            "sessionKey": "sk-empty",
            "runId": "run-empty",
            "data": {"phase": "end"}
        })

        cb.assert_called_once_with("sk-empty", "")

    def test_cleanup_on_lifecycle_end(self, fake_glib):
        """Buffer is cleared after lifecycle end."""
        from ui.handlers.activity_handler import ActivityHandler
        handler = ActivityHandler(
            feedbar=MagicMock(), main_content=MagicMock(), GLib_module=fake_glib
        )

        handler.on_gateway_event("agent", {
            "stream": "assistant", "sessionKey": "sk-1", "runId": "run-1",
            "data": {"text": "Hello"}
        })
        handler.on_gateway_event("agent", {
            "stream": "lifecycle", "sessionKey": "sk-1", "runId": "run-1",
            "data": {"phase": "end"}
        })

        assert handler._assistant_text_buffer.get("sk-1") is None

    def test_cleanup_on_lifecycle_error(self, fake_glib):
        """Buffer is also cleared on lifecycle error (memory leak fix)."""
        from ui.handlers.activity_handler import ActivityHandler
        handler = ActivityHandler(
            feedbar=MagicMock(), main_content=MagicMock(), GLib_module=fake_glib
        )

        handler.on_gateway_event("agent", {
            "stream": "assistant", "sessionKey": "sk-1", "runId": "run-err",
            "data": {"text": "Hello"}
        })
        handler.on_gateway_event("agent", {
            "stream": "lifecycle", "sessionKey": "sk-1", "runId": "run-err",
            "data": {"phase": "error"}
        })

        assert handler._assistant_text_buffer.get("sk-1") is None


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


class TestSetOnAssistantBuffer:
    """set_on_assistant_buffer() wiring."""

    def test_setter_stores_callback(self, fake_glib):
        from ui.handlers.activity_handler import ActivityHandler
        handler = ActivityHandler(
            feedbar=MagicMock(), main_content=MagicMock(), GLib_module=fake_glib
        )
        cb = MagicMock()
        handler.set_on_assistant_buffer(cb)
        assert handler._on_assistant_buffer is cb

    def test_no_crash_when_callback_not_set(self, fake_glib):
        """No crash when on_assistant_buffer is None."""
        from ui.handlers.activity_handler import ActivityHandler
        handler = ActivityHandler(
            feedbar=MagicMock(), main_content=MagicMock(), GLib_module=fake_glib
        )
        # Don't set callback — should not crash
        handler.on_gateway_event("agent", {
            "stream": "assistant", "sessionKey": "sk-1", "runId": "run-1",
            "data": {"text": "Hello"}
        })

    def test_no_crash_when_lifecycle_callback_not_set(self, fake_glib):
        """No crash when lifecycle_completed_callback is None."""
        from ui.handlers.activity_handler import ActivityHandler
        handler = ActivityHandler(
            feedbar=MagicMock(), main_content=MagicMock(), GLib_module=fake_glib
        )
        # Don't set lifecycle callback — should not crash on lifecycle end
        handler.on_gateway_event("agent", {
            "stream": "lifecycle", "sessionKey": "sk-1", "runId": "run-1",
            "data": {"phase": "end"}
        })


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

    def test_agent_start_fires_clear_guard_callback(self, fake_glib):
        """on_agent_start fires the callback that clears ChatHandler's guard."""
        from ui.handlers.activity_handler import ActivityHandler
        mc = MagicMock()
        mc.get_current_session_key = MagicMock(return_value=None)
        handler = ActivityHandler(
            feedbar=MagicMock(), main_content=mc, GLib_module=fake_glib
        )
        cleared = []
        handler.set_on_agent_start(lambda sk: cleared.append(sk))

        handler.on_gateway_event("agent", {
            "stream": "lifecycle", "sessionKey": "sk-1", "runId": "run-1",
            "data": {"phase": "start"}
        })

        assert "sk-1" in cleared

    def test_multiple_rounds_all_render(self, fake_glib):
        """End-to-end: multiple rounds to the same session all render bubbles."""
        from ui.handlers.activity_handler import ActivityHandler
        from ui.handlers.chat_handler import ChatHandler

        mc = MagicMock()
        mc.get_current_session_key = MagicMock(return_value=None)
        mc.get_chat_box_for_session.return_value = MagicMock()

        ah = ActivityHandler(feedbar=MagicMock(), main_content=mc, GLib_module=fake_glib)
        ch = ChatHandler(
            main_content=mc,
            agent_to_project=MagicMock(),
            projects_module=MagicMock(),
            GLib_module=fake_glib,
        )
        ch._agent_to_project.get_project.return_value = None
        fake_box = MagicMock()
        mc.get_chat_box_for_session.return_value = fake_box
        fake_render = MagicMock()
        fake_render.is_streaming.return_value = False
        fake_render.render_sync.return_value = MagicMock()
        ch._chat_render_handler = fake_render

        ah.set_on_assistant_buffer(ch._buffer_assistant_text)
        ah.set_on_lifecycle_completed(ch._handle_lifecycle_completed)
        ah.set_on_agent_start(ch._clear_render_guard)

        for i in range(3):
            ah.on_gateway_event("agent", {
                "stream": "lifecycle", "sessionKey": "sk-1", "runId": f"run-{i}",
                "data": {"phase": "start"}
            })
            ah.on_gateway_event("agent", {
                "stream": "assistant", "sessionKey": "sk-1", "runId": f"run-{i}",
                "data": {"text": f"Response {i}"}
            })
            ah.on_gateway_event("agent", {
                "stream": "lifecycle", "sessionKey": "sk-1", "runId": f"run-{i}",
                "data": {"phase": "end"}
            })
            ch.on_chat_event("chat", {
                "state": "final", "sessionKey": "sk-1", "runId": f"run-{i}",
                "message": {"content": [{"type": "text", "text": f"Response {i}"}]}
            })

        assert fake_box.append.call_count == 3, f"Expected 3 renders, got {fake_box.append.call_count}"