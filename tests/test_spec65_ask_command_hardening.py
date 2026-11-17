"""SPEC-65: /ask parser, prompt alignment, registry sync, and relay hardening."""

from __future__ import annotations

# GTK_HEADLESS_SAFE: this module constructs NO widgets. It imports
# ui.window (whose module contains Gtk.* constructor calls) only for an
# unbound-method call on a duck-typed host; the conftest AST guard would
# otherwise skip all 13 tests headless, hiding coverage and breaking
# acceptance rows R-10/R-11 (runner: plain). (Placed after the __future__
# import: Python allows only docstring/comments/blank lines before it.)
GTK_HEADLESS_SAFE = True

import threading
import unittest.mock
from pathlib import Path

from models.command import CommandRegistry
from ui.handlers.agent_command_handler import (
    AgentCommandHandler,
    _extract_quoted_commands,
)
from ui.handlers.collab_handler import CollabHandler
from ui.handlers.command_handler import CommandHandler
from ui.window import MainWindow


def _cmds(text: str, names: set[str] | None = None):
    return _extract_quoted_commands(text, names)


class TestParserAndOrdering:
    """Findings 2A, 2B, 2C, and 3D: what the scanner accepts, drops, and orders."""

    def test_prompt_formatting_quotes_alias_and_inner_quotes(self):
        cases = [
            ('`/ask @Debugger "q"`', "q"),
            ('**/ask @Debugger "q"**', "q"),
            ('- /ask @Debugger "q"', "q"),
            ('> /ask @Debugger "q"', "q"),
            ('/ask @Debugger “q”', "q"),
            ("/ask @Debugger 'q'", "q"),
            ('/ask @Debugger — "q"', "q"),
            ('/ask @Debugger: "q"', "q"),
            ('/a @Debugger "q"', "q"),
            ('/ask @Debugger "check "foo" in bar.py"', 'check "foo" in bar.py'),
            ('/ask @Debugger ""foo" is broken"', '"foo" is broken'),
            # Flagger BUG #4 (continuation clause): `\` followed by `\n` is an
            # unknown escape — _scan_agent_quoted_payload keeps the backslash
            # and the newline verbatim (no whitespace joining). Pin exactly
            # that documented behavior (docstring: "\\ \n continuations").
            ('/ask @Debugger "line1 \\\nline2"', 'line1 \\\nline2'),
        ]
        for text, payload in cases:
            found = _cmds(text)
            assert found == [found[0]] if found else False
            assert len(found) == 1, text
            assert found[0].command == "ask", text
            assert found[0].agent == "@Debugger", text
            assert found[0].payload == payload, text

    def test_think_fence_html_and_unquoted_are_dropped_or_unwrapped(self):
        # Newline-separated: the inner /ask IS discoverable (preceded by \n), so
        # only _strip_thinking_blocks can remove it. With the strip deleted this
        # returns TWO commands (R-10's think-strip half — verified by probe).
        got = _cmds('<think>\n/ask @Coder "draft"\n</think>\n/ask @Debugger "real"')
        assert [c.payload for c in got] == ["real"]
        assert _cmds("<think>\n/ask @Coder \"never closed") == []
        assert _cmds('```python\n/ask @Debugger "no"\n```\n/ask @Debugger "yes"')[0].payload == "yes"
        html = (
            "```html\n"
            '<code>/ask @Coder "example"</code>\n'
            '<p>/ask @Debugger "real"</p>\n'
            "```"
        )
        found = _cmds(html)
        assert len(found) == 1
        assert found[0].agent == "@Debugger"
        assert found[0].payload == "real"
        # <pre> mirrors <code>: both are excluded from unwrapped ```html cards
        # (AC §6 Finding 2C names <code>/<pre>; only <p> content is live).
        pre_html = (
            "```html\n"
            '<pre>/ask @Coder "example"</pre>\n'
            '<p>/ask @Debugger "real"</p>\n'
            "```"
        )
        pre_found = _cmds(pre_html)
        assert len(pre_found) == 1
        assert pre_found[0].agent == "@Debugger"
        assert pre_found[0].payload == "real"
        assert _cmds('/ask @QTR unquoted') == []
        assert _cmds('/ask @QTR"no space"') == []
        assert _cmds("```\n/ask @Debugger \"inside\"\n```") == []
        assert _cmds("") == []
        assert _cmds(None) == []  # type: ignore[arg-type]

    def test_raw_start_monotonic_and_source_ordered(self):
        # Pin raw_start as a truthful pointer INTO the ORIGINAL text, the
        # invariant the results.sort(key=raw_start) defends (Debugger #2 /
        # Flagger BUG #1 half 1). Acceptance row R-10 deletes that sort; a
        # single forward scan yields sorted output for plain inputs anyway,
        # so this pin is documentation-grade — the R-10 mutation's detection
        # load now rides on the think-strip fixture above (which IS
        # discriminating).
        text = '/ask @Coder "q1" /ask @Debugger "q2"\n/ask @Supervisor "q3"'
        got = _cmds(text)
        assert [c.payload for c in got] == ["q1", "q2", "q3"]
        starts = [c.raw_start for c in got]
        assert starts == sorted(starts)
        for c in got:
            # raw_start points at the prefix boundary, which may be the ONE
            # whitespace/backtick char _CMD_PREFIX_RE consumed before the
            # slash — so assert the span LOCATES the command in the original
            # text rather than that it starts exactly at "/ask".
            span = text[c.raw_start:c.raw_end]
            assert f'/{c.command} {c.agent} "{c.payload}"' in span
            assert span.endswith(f'"{c.payload}"')
        # Spans are non-overlapping (occupied-spans guard) and contiguous
        # with only separators between them.
        for prev, nxt in zip(got, got[1:]):
            assert prev.raw_end <= nxt.raw_start

    def test_clear_then_ask_keeps_source_order_and_hides_nested_clear(self):
        found = _cmds('/clear @Debugger\n/ask @Debugger "go"')
        assert [c.command for c in found] == ["clear", "ask"]
        found = _cmds('/compact @Debugger\n/ask @Debugger "go"')
        assert [c.command for c in found] == ["compact", "ask"]
        nested = _cmds('/ask @Debugger "do not run /clear @Coder"')
        assert len(nested) == 1
        assert nested[0].command == "ask"
        pair = _cmds('/ask @Debugger "q1" /ask @Coder "q2"')
        assert [c.payload for c in pair] == ["q1", "q2"]
        note = _cmds('/ask @Debugger "is this right?" (see "note")')
        assert note[0].payload == "is this right?"

    def test_process_input_keeps_newlines_and_strips_mention_punctuation(self):
        handler = CommandHandler(
            agent_manager=None,
            project_handler=None,
            collab_handler=CollabHandler(),
        )
        handler.set_special_agents({"agent:debugger": "Debugger"})
        assert "a" in handler.get_command_names()
        result = handler.process_input(
            "agent:coder",
            '/ask @Debugger "line1\n  indented"',
            skip_dispatch=True,
        )
        assert result.handled is True
        assert result.forward_to == "agent:debugger"
        assert result.forward_text == "line1\n  indented"
        punct = handler.process_input(
            "agent:coder",
            '/ask @Debugger: "after colon"',
            skip_dispatch=True,
        )
        assert punct.forward_to == "agent:debugger"
        assert punct.forward_text == "after colon"
        bad = handler.process_input(
            "agent:coder",
            '/ask @Debugger "unclosed',
            skip_dispatch=True,
        )
        assert bad.handled is True
        assert bad.forward_to is None
        assert "Unclosed quote" in (bad.response_text or "")
        empty = handler.process_input(
            "agent:coder",
            '/ask @Debugger ""',
            skip_dispatch=True,
        )
        assert "Empty payload" in (empty.response_text or "")
        unknown = handler.process_input(
            "agent:coder",
            '/ask @: "no name"',
            skip_dispatch=True,
        )
        assert "Unknown agent" in (unknown.response_text or "")

    def test_resolve_name_sad_paths(self):
        reg = CommandRegistry()
        reg.register("ask", lambda cmd: cmd, aliases=["a"])
        assert reg.resolve_name("A") == "ask"
        assert reg.resolve_name("ask") == "ask"
        assert reg.resolve_name("missing") is None
        assert reg.resolve_name("") is None
        assert reg.resolve_name(None) is None  # type: ignore[arg-type]

    def test_prompts_teach_plain_ask_lines(self):
        with open("prompts/system/collab.md", encoding="utf-8") as collab_file:
            collab = collab_file.read()
        with open("prompts/system/crabcakes-commands.md", encoding="utf-8") as commands_file:
            commands = commands_file.read()
        assert "Agent Collaboration" in collab
        assert "`/ask" not in collab
        assert "```" not in collab
        assert '/ask @AgentName "question"' in collab
        assert "/review" not in commands
        assert "/check" not in commands
        assert "/accept" not in commands
        assert "/reject" not in commands
        assert "Alias `/st`" in commands
        assert "Alias `/s`." in commands
        assert "/clear" in commands
        assert "/compact" in commands
        assert "/session" in commands

    def test_crabcakes_commands_doc_matches_live_registry(self):
        # Flagger BUG #7: every live command AND alias must appear in the
        # shipped commands doc. This handler registers help/ask/stop (the
        # project_handler=None branch skips status/agents/cost/clear/compact),
        # so it pins exactly the four tokens the doc must carry for this
        # construction.
        from pathlib import Path

        handler = CommandHandler(
            agent_manager=None,
            project_handler=None,
            collab_handler=CollabHandler(),
        )
        live = handler.get_command_names()
        # help + ask + stop register unconditionally; "?" and "a" are their aliases.
        assert live == {"help", "?", "ask", "a", "stop"}
        doc = Path("prompts/system/crabcakes-commands.md").read_text(encoding="utf-8")
        for name in sorted(live):
            assert f"`/{name}`" in doc, f"doc missing live command /{name}"


class _SendRuntime:
    def __init__(self, specials, inbound=False):
        self._specials = specials
        self.sent = []
        self.inbound = inbound
        self._lock = threading.Lock()
        self.conv = unittest.mock.MagicMock()

    def get_special_agents(self):
        return self._specials

    def send_to_special_agent(self, sk, text):
        self.sent.append((sk, text))

    def has_pending_ask_inbound(self, session_key):
        return self.inbound

    def get_special_agent_def(self, session_key):
        if session_key not in self._specials:
            return None
        defn = unittest.mock.MagicMock()
        defn.display_name = self._specials[session_key]
        return defn

    def _get_runtime(self, display_name, agent_def=None):
        return self

    def get_conversation(self, session_key):
        return self.conv


class TestRelayAndRegistrySync:
    """Findings 3A, 3B, and 3C: stranded asks, chain depth, and live registry."""

    def test_stranded_inbound_keeps_pending_ask_until_the_next_turn(self):
        rt = _SendRuntime(
            {"agent:debugger": "Debugger", "agent:supervisor": "Supervisor"},
            inbound=True,
        )
        handler = AgentCommandHandler()
        handler.set_command_handler(type("C", (), {"get_command_names": lambda self: {"ask"}})())
        handler.set_agent_runtime_handler(rt)
        handler._pending_asks["agent:debugger"] = "agent:supervisor"
        handler.on_agent_response("agent:debugger", "finished the earlier turn", None)
        assert handler._pending_asks["agent:debugger"] == "agent:supervisor"
        assert rt.sent == []

        rt.inbound = False
        handler.on_agent_response("agent:debugger", "the real answer", None)
        assert "agent:debugger" not in handler._pending_asks
        assert rt.sent[0][0] == "agent:supervisor"
        assert rt.sent[0][1].startswith("[Debugger responded]: the real answer")

    def test_second_asker_waits_and_error_clears_a_zombie_ask(self):
        rt = _SendRuntime({"agent:debugger": "Debugger", "agent:supervisor": "Supervisor", "agent:coder": "Coder"})
        handler = AgentCommandHandler()
        handler.set_agent_runtime_handler(rt)
        handler._push_pending_ask("agent:debugger", "agent:supervisor")
        handler._push_pending_ask("agent:debugger", "agent:coder")
        assert handler._pending_asks["agent:debugger"] == "agent:supervisor"
        assert handler._pending_ask_waiters["agent:debugger"] == ["agent:coder"]
        first = handler._pop_next_asker("agent:debugger")
        assert first == "agent:supervisor"
        assert handler._pending_asks["agent:debugger"] == "agent:coder"
        handler.on_agent_error("agent:debugger", "provider down")
        assert "agent:debugger" not in handler._pending_asks
        rt.conv.add_user_message.assert_called()
        note = rt.conv.add_user_message.call_args.args[0]
        assert "provider down" in note
        handler._pending_asks["agent:debugger"] = "agent:supervisor"
        rt.inbound = True
        handler.on_agent_error("agent:debugger", "still queued")
        assert handler._pending_asks["agent:debugger"] == "agent:supervisor"
        handler.on_agent_error("", "ignored")
        handler.on_agent_error(None, "ignored")  # type: ignore[arg-type]

    def test_chain_depth_clears_when_handler_is_wired_and_self_ask_keeps_it(self):
        handler = AgentCommandHandler()
        rt = _SendRuntime({"agent:coder": "Coder", "agent:debugger": "Debugger"})
        handler.set_agent_runtime_handler(rt)

        class _Cmd:
            def get_command_names(self):
                return {"ask"}

            def process_input(self, sk, text, skip_dispatch=False):
                from models.command import CommandResult
                if "@Coder" in text:
                    return CommandResult(handled=True, forward_to="Coder", forward_text="step")
                return CommandResult(handled=True, forward_to="Debugger", forward_text="q")

        handler.set_command_handler(_Cmd())
        handler._chain_depth["agent:coder"] = 2
        routed = handler.on_agent_response("agent:coder", "no commands here", None)
        assert routed == set()
        assert "agent:coder" not in handler._chain_depth

        routed = handler.on_agent_response("agent:coder", '/ask @Coder "step 2"', None)
        assert "agent:coder" in routed
        assert handler._chain_depth["agent:coder"] == 1

    def test_think_wrapped_audit_report_is_not_scanned(self):
        # Flagger BUG #4 — AC 2B audit half: _process_audit_reports strips
        # <think> BEFORE extract_audit_reports, so an audit report inside a
        # think block must NOT be logged or card-emitted, while the live
        # /ask after the block still routes (mirrors test_agent_command_
        # handler.py::test_audit_report_emits_feed_card_callback).
        handler = AgentCommandHandler()
        received = []
        handler.set_on_audit_report(lambda r: received.append(r))
        handler.set_project_path_provider(lambda: "/nonexistent-s65-twap")

        class _Cmd:
            def get_command_names(self):
                return {"ask"}

            def process_input(self, sk, text, skip_dispatch=False):
                from models.command import CommandResult
                target = "Supervisor" if "@Supervisor" in text else "Debugger"
                return CommandResult(handled=True, forward_to=target, forward_text="done")

        handler.set_command_handler(_Cmd())
        rt = _SendRuntime({"agent:debugger": "Debugger", "agent:supervisor": "Supervisor"})
        handler.set_agent_runtime_handler(rt)
        handler.on_agent_response(
            "agent:debugger",
            "<think>\n## AUDIT REPORT\nfinding X\n</think>\n/ask @Supervisor \"done\"",
            None,
        )
        # Audit half: no review-log write, no feed-card callback.
        assert received == []
        assert (Path("/nonexistent-s65-twap") / ".crabcakes" / "review-log.jsonl").exists() is False
        # Command half: the live ask after the think block still routed.
        assert handler._pending_asks["agent:supervisor"] == "agent:debugger"
        assert rt.sent[0][0] == "agent:supervisor"
        assert rt.sent[0][1].startswith("[Debugger asks]: done")

    def test_clear_then_ask_executes_in_source_order(self):
        # Flagger BUG #8 — execution-order half of the AC: /clear must be
        # dispatched before /ask for the SAME target (AC §6 Finding 3D). The
        # scan already yields source order (raw_start); this pins the
        # process_input call order through a recording CommandHandler.
        handler = AgentCommandHandler()
        calls: list[tuple[str, str]] = []
        chained_ask_forward: list[tuple[str, str]] = []

        class _RecordingCmd:
            def get_command_names(self):
                return {"ask", "clear"}

            def process_input(self, sk, text, skip_dispatch=False):
                from models.command import CommandResult
                calls.append((sk, text))
                if text.startswith("/clear"):
                    return CommandResult(handled=True, response_text="Cleared Debugger's conversation.")
                return CommandResult(handled=True, forward_to="Debugger", forward_text="go")

        handler.set_command_handler(_RecordingCmd())
        rt = _SendRuntime({"agent:debugger": "Debugger", "agent:supervisor": "Supervisor"})
        handler.set_agent_runtime_handler(rt)
        handler.on_agent_response("agent:supervisor", '/clear @Debugger\n/ask @Debugger "go"', None)

        command_calls = [t for _sk, t in calls if t.startswith(("/clear", "/ask"))]
        assert command_calls == ['/clear @Debugger', '/ask @Debugger "go"']

        # /clear leaves NO pending ask (payload-free action); the ask routes
        # to Debugger with the asker prefix.
        assert handler._pending_asks == {"agent:debugger": "agent:supervisor"}
        assert chained_ask_forward == []
        assert rt.sent[0][0] == "agent:debugger"
        assert rt.sent[0][1].startswith("[Supervisor asks]: go")

    def test_sync_special_agent_consumers_updates_command_handler(self):
        calls = {}

        class _Panel:
            def set_special_agents(self, arh):
                calls["panel"] = arh

        class _Cmd:
            def set_special_agents(self, agents):
                calls["cmd"] = dict(agents)

        class _Arh:
            def get_special_agents(self):
                return {"agent:debugger": "Debugger"}

        host = type("Host", (), {})()
        host._agent_runtime_handler = _Arh()
        host._left_panel = _Panel()
        host._command_handler = _Cmd()
        MainWindow._sync_special_agent_consumers(host)
        assert calls["panel"] is host._agent_runtime_handler
        assert calls["cmd"] == {"agent:debugger": "Debugger"}

        host._agent_runtime_handler = None
        MainWindow._sync_special_agent_consumers(host)
        host._left_panel = None
        host._command_handler = None
        host._agent_runtime_handler = _Arh()
        MainWindow._sync_special_agent_consumers(host)

    def test_room_witness_skips_routed_ask_target(self):
        from ui.handlers.agent_runtime_handler import AgentRuntimeHandler

        crh = unittest.mock.MagicMock()
        crh.is_streaming.return_value = False
        crh.get_streaming_text.return_value = ""
        handler = AgentRuntimeHandler(unittest.mock.MagicMock(), crh, None)
        handler._active_project = ["proj"]
        witnessed = []
        handler.record_room_line = lambda member, line: witnessed.append(member)  # type: ignore[method-assign]
        handler.take_room_turn = lambda sk, token: True  # type: ignore[method-assign]
        handler._on_room_members = lambda project: ["agent:supervisor", "agent:coder", "agent:debugger"]
        handler._on_agent_response = lambda sk, text, project: {"agent:coder"}
        token = object()
        handler._turn_tokens["agent:supervisor"] = token
        handler._do_response_complete("agent:supervisor", "done", token)
        assert "agent:coder" not in witnessed
        assert "agent:debugger" in witnessed
        assert "agent:supervisor" not in witnessed

    def test_error_callback_fires_and_clear_compact_guards(self):
        from ui.agent_runtime.session import RuntimeSessionMixin
        from ui.handlers.agent_runtime_handler import AgentRuntimeHandler

        crh = unittest.mock.MagicMock()
        handler = AgentRuntimeHandler(unittest.mock.MagicMock(), crh, None)
        seen = []
        handler.set_on_agent_error(lambda sk, msg: seen.append((sk, msg)))
        handler._do_error("agent:coder", "boom")
        assert seen and seen[0][0] == "agent:coder"
        assert "boom" in seen[0][1]

        class _Agent:
            display_name = "Debugger"

        class _Rt:
            def is_loop_active(self, sk):
                return True

            def get_conversation(self, sk):
                raise AssertionError("refused clear must not touch the conversation")

        class _Host(RuntimeSessionMixin):
            def __init__(self):
                self._agents = {"agent:debugger": _Agent()}
                self._turn_attr = {"agent:debugger": {"turn": 1}}
                self._runtimes = {"Debugger": _Rt()}

            def _get_runtime(self, name, agent_def=None):
                return _Rt()

        host = _Host()
        assert host.clear_conversation("agent:debugger") is False
        assert host._turn_attr["agent:debugger"] == {"turn": 1}
        compact = host.compact_conversation("agent:debugger")
        assert compact["error"] == "tool loop is active"
        assert compact["messages_removed"] == 0
        assert host.has_pending_ask_inbound("") is False
        assert host.has_pending_ask_inbound(None) is False  # type: ignore[arg-type]

        class _Peek:
            def peek_pending_inbound(self, sk):
                return ["[Supervisor asks]: look here"]

        host._runtimes = {"Debugger": _Peek()}
        assert host.has_pending_ask_inbound("agent:debugger") is True


def test_cmd_compact_refuses_active_loop_message():
    from models.command import Command
    from ui.handlers.project_handler import ProjectHandler

    ph = ProjectHandler.__new__(ProjectHandler)
    ph._compact_callback = lambda sk, focus: {"error": "tool loop is active", "messages_removed": 3}
    ph._compact_chat_callback = None
    cmd = Command(name="compact", source_session_key="agent:debugger", body="")
    result = ph.cmd_compact(cmd)
    assert result.handled is True
    assert "tool loop is currently running" in (result.response_text or "")
    assert "Removed 3" not in (result.response_text or "")
