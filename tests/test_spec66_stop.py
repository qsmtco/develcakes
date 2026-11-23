"""SPEC-66 §2.8/§2.9 — /stop performs a real cancel (R-18, R-18b).

SPLIT runners (§12.7):
  * ``test_stop_returns_cancel_session_key_and_does_not_forward`` — plain.
    Imports only models.command, ui.handlers.collab_handler and
    ui.handlers.chat_handler (no GTK).
  * ``test_cancel_agent_kills_process_group`` — xvfb. Touches
    ui.handlers.agent_runtime_handler.

Neither node imports ui.window.

SPLIT runners in ONE file (§12.7) means the conftest widget guard — which
scans per FILE — would skip this whole module when no display exists, taking
the plain R-18 node with it. Both halves are headless-safe (they import GTK
modules but never construct a widget: every object comes from ``__new__`` or
``SimpleNamespace``), so the module declares it per §12.7 rule (b). R-18b
still runs under xvfb in the gate, where a display is present anyway.
"""
from __future__ import annotations

GTK_HEADLESS_SAFE = True

import types

from models.command import Command, CommandResult


def _stop_cmd(target: str | None) -> Command:
    return Command(name="stop", args=[f"@{target}"] if target else [],
                   target_session_key=target)


class TestStopIsACancel:
    """R-18 (plain): /stop reports the session key and forwards nothing."""

    def test_stop_returns_cancel_session_key_and_does_not_forward(self):
        """CollabHandler.cmd_stop sets cancel_session_key and no forward pair."""
        from ui.handlers.collab_handler import CollabHandler

        result = CollabHandler().cmd_stop(_stop_cmd("agent:coder"))
        assert isinstance(result, CommandResult)
        assert result.handled is True
        assert result.cancel_session_key == "agent:coder"
        # A forwarded "stop" string used to arrive as chat text, not a cancel.
        assert result.forward_to is None
        assert result.forward_text is None

    def test_stop_without_a_target_asks_for_one(self):
        from ui.handlers.collab_handler import CollabHandler

        result = CollabHandler().cmd_stop(_stop_cmd(None))
        assert result.handled is True
        assert result.cancel_session_key is None
        assert result.forward_to is None
        assert "Usage: /stop @agent" in (result.response_text or "")

    def test_chat_handler_cancels_and_never_sends_local(self, monkeypatch):
        """ChatHandler.on_send fires the sink and calls NO _send_local."""
        from ui.handlers.chat_handler import ChatHandler

        handler = ChatHandler(
            main_content=types.SimpleNamespace(
                get_current_session_key=lambda: "agent:supervisor",
                user_input=_FakeBuffer("🛑 /stop @agent:coder"),
            ),
            agent_to_project=None,
            projects_module=None,
            GLib_module=None,
        )

        class _CmdHandler:
            def process_input(self, session_key, text):
                return CommandResult(
                    handled=True,
                    cancel_session_key="agent:coder",
                    response_text="Stopping agent:coder",
                )

        handler.set_command_handler(_CmdHandler())
        cancelled: list[str] = []
        handler.set_on_cancel_session(cancelled.append)
        sent: list[tuple] = []
        monkeypatch.setattr(
            handler, "_send_local", lambda *a, **k: sent.append((a, k))
        )

        handler.on_send()

        assert cancelled == ["agent:coder"]
        assert sent == [], "ChatHandler must not forward a 'stop' send"

    def test_chat_handler_without_a_sink_says_stop_is_not_wired(self, monkeypatch):
        from ui.handlers.chat_handler import ChatHandler

        handler = ChatHandler(
            main_content=types.SimpleNamespace(
                get_current_session_key=lambda: "agent:supervisor",
                user_input=_FakeBuffer("🛑 /stop @agent:coder"),
            ),
            agent_to_project=None,
            projects_module=None,
            GLib_module=None,
        )

        class _CmdHandler:
            def process_input(self, session_key, text):
                return CommandResult(
                    handled=True,
                    cancel_session_key="agent:coder",
                    response_text="Stopping agent:coder",
                )

        handler.set_command_handler(_CmdHandler())
        shown: list[tuple] = []
        handler._chat_render_handler = types.SimpleNamespace(
            render_sync=lambda *a: shown.append(a)
        )
        sent: list[tuple] = []
        monkeypatch.setattr(
            handler, "_send_local", lambda *a, **k: sent.append((a, k))
        )

        handler.on_send()

        assert sent == []
        assert any("Stop is not wired." in str(call) for call in shown)

    def test_agent_command_loop_continues_past_the_cancel(self):
        """§2.8(d): the cancel branch counts, CONTINUES, and does not route.

        Constructed with __new__ (the §2.8 getattr guard) so the class
        attribute set is exactly what the test provides.
        """
        from ui.handlers.agent_command_handler import AgentCommandHandler

        handler = AgentCommandHandler.__new__(AgentCommandHandler)
        handler._chain_depth = {}
        handler._pending_asks = {}
        handler._pending_ask_waiters = {}

        calls: list[tuple[str, str]] = []

        class _CmdHandler:
            def get_command_names(self):
                return {"stop", "ask"}

            def process_input(self, session_key, text, skip_dispatch=False):
                calls.append((session_key, text))
                if text.startswith("/stop"):
                    return CommandResult(
                        handled=True,
                        cancel_session_key="agent:coder",
                        response_text="Stopping agent:coder",
                    )
                return CommandResult(
                    handled=True,
                    forward_to="agent:debugger",
                    forward_text="ping",
                )

        handler._command_handler = _CmdHandler()
        cancelled: list[str] = []
        handler.set_on_cancel_session(cancelled.append)
        routed: list[tuple] = []
        handler._route_command = lambda result, project, depth, **kw: routed.append(
            (result.forward_to, result.forward_text)
        ) or result.forward_to
        results: list[str] = []
        handler._record_action_result = lambda sk, text: results.append(text)
        handler._process_audit_reports = lambda *a, **k: None
        handler._has_unstarted_ask_for = lambda sk: False

        text = '/stop @Coder\n/ask @Debugger "ping"'
        handler.on_agent_response("agent:supervisor", text, None)

        # The cancel ran...
        assert cancelled == ["agent:coder"]
        # ...and the SECOND command was not dropped by a break.
        assert routed == [("agent:debugger", "ping")]

    def test_cancel_branch_without_a_sink_falls_through(self):
        """__new__ doubles have no _on_cancel_session — the getattr guard holds."""
        from ui.handlers.agent_command_handler import AgentCommandHandler

        handler = AgentCommandHandler.__new__(AgentCommandHandler)
        handler._chain_depth = {}
        handler._pending_asks = {}
        handler._pending_ask_waiters = {}
        assert getattr(handler, "_on_cancel_session", None) is None

        class _CmdHandler:
            def get_command_names(self):
                return {"stop"}

            def process_input(self, session_key, text, skip_dispatch=False):
                return CommandResult(
                    handled=True,
                    cancel_session_key="agent:coder",
                    response_text="Stopping agent:coder",
                )

        handler._command_handler = _CmdHandler()
        results: list[str] = []
        handler._record_action_result = lambda sk, text: results.append(text)
        handler._process_audit_reports = lambda *a, **k: None
        handler._has_unstarted_ask_for = lambda sk: False

        handler.on_agent_response(
            "agent:supervisor", "/stop @Coder", None
        )
        # No sink: the outcome is recorded rather than an AttributeError.
        assert results


class TestCancelAgent:
    """R-18b (xvfb): AgentRuntimeHandler.cancel_agent drives rt.cancel."""

    def test_cancel_agent_kills_process_group(self):
        from ui.handlers.agent_runtime_handler import AgentRuntimeHandler

        handler = AgentRuntimeHandler.__new__(AgentRuntimeHandler)
        handler._agents = {
            "agent:coder": types.SimpleNamespace(display_name="Coder"),
        }
        cancelled: list[str] = []

        def _cancel(session_key):
            cancelled.append(session_key)
            return (2, 0)

        rt = types.SimpleNamespace(
            cancel=_cancel,
            get_conversation=lambda sk: object(),
        )
        handler._runtimes = {"Coder": rt}

        outcome = handler.cancel_agent("agent:coder")

        assert cancelled == ["agent:coder"]
        assert outcome == "cancelled+killed:2"

    def test_cancel_agent_unknown_and_unstarted_are_honest(self):
        from ui.handlers.agent_runtime_handler import AgentRuntimeHandler

        handler = AgentRuntimeHandler.__new__(AgentRuntimeHandler)
        handler._agents = {
            "agent:coder": types.SimpleNamespace(display_name="Coder"),
        }
        handler._runtimes = {}

        assert handler.cancel_agent("agent:nobody") == "unknown"
        assert handler.cancel_agent("agent:coder") == "no_runtime"

    def test_idle_cancel_reports_unkillable_suffix(self):
        from ui.handlers.agent_runtime_handler import AgentRuntimeHandler

        handler = AgentRuntimeHandler.__new__(AgentRuntimeHandler)
        handler._agents = {
            "agent:coder": types.SimpleNamespace(display_name="Coder"),
        }
        handler._runtimes = {
            "Coder": types.SimpleNamespace(
                cancel=lambda sk: (0, 1),
                get_conversation=lambda sk: object(),
            )
        }

        assert handler.cancel_agent("agent:coder") == "cancelled+1-unkillable"


class _FakeBuffer:
    """Just enough of a GtkTextView + GtkTextBuffer for on_send's read + clear.

    ``on_send`` reaches ``user_input.get_buffer()`` and then iterates with
    ``get_start_iter``/``get_end_iter``, so the widget double IS its own
    buffer — one object, no GTK.
    """

    def __init__(self, text: str):
        self._text = text

    def get_buffer(self):
        return self

    def get_start_iter(self):
        return None

    def get_end_iter(self):
        return None

    def get_text(self, _start, _end, _include_hidden):
        return self._text

    def set_text(self, text: str):
        self._text = text
