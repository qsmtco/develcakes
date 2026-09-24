# tests/test_agent_command_handler.py
# Tests for ui/handlers/agent_command_handler.py — Phase 6.2.
#
# Principle: mock at the boundary, test behavior not internals.
# All tests use FakeCommandHandler, FakeAgentRuntimeHandler (gateway fakes
# deleted with the R1 gateway branch, SPEC-05 SP2).
# to isolate AgentCommandHandler logic from the rest of the codebase.
#
# Architecture §8.6 handler pattern: receives all deps via setters,
# never imports from ui/handlers/. Tests follow the same isolation.

import pytest

from models.command import CommandResult


# ── Fake dependencies (mirrors spec §7 helpers) ──────────────────────────────────


class FakeCommandHandler:
    """Mock CommandHandler — records calls and returns configurable results."""

    def __init__(self, commands=None):
        self._commands = commands or {"ask", "tell", "delegate"}
        self.processed = []       # [(session_key, text), ...]
        self._forward_map = {}    # override forward routing for tests

    def get_command_names(self):
        return set(self._commands)

    def set_forward_map(self, mapping: dict):
        """Set explicit forward mapping for tests: text → CommandResult."""
        self._forward_map = mapping

    def process_input(self, session_key, text, skip_dispatch=False):
        self.processed.append((session_key, text))
        if text in self._forward_map:
            return self._forward_map[text]
        # Default: parse /ask @Target message and /tell @Target message
        if text.startswith("/ask @"):
            rest = text[6:]
            parts = rest.split(" ", 1)
            target = parts[0]
            msg = parts[1]if len(parts) > 1 else ""
            return CommandResult(handled=True, forward_to=target, forward_text=msg)
        if text.startswith("/tell @"):
            rest = text[7:]
            parts = rest.split(" ", 1)
            target = parts[0]
            msg = parts[1]if len(parts) > 1 else ""
            return CommandResult(handled=True, forward_to=target, forward_text=msg)
        if text.startswith("/delegate @"):
            rest = text[10:]
            parts = rest.split(" ", 1)
            target = parts[0].lstrip("@")  # strip @ prefix from target name
            msg = parts[1]if len(parts) > 1 else ""
            return CommandResult(handled=True, forward_to=target, forward_text=msg)
        return CommandResult(handled=False)


class FakeAgentRuntimeHandler:
    """Mock AgentRuntimeHandler — records special agent sends."""

    def __init__(self, special_agents=None):
        self._agents = special_agents or {}
        self.sent = []  # [(session_key, text), ...]

    def get_special_agents(self):
        return self._agents

    def send_to_special_agent(self, sk, text):
        self.sent.append((sk, text))


class FakeAgentManager:
    """Mock AgentManager — provides display name resolution."""

    def __init__(self, names=None):
        self._names = names or {}  # {session_key: display_name}

    def get_name(self, sk):
        return self._names.get(sk, sk.split("/")[-1])


class FakeRoutingTable:
    """Mock AgentRoutingTable — provides project→agent routing."""

    def __init__(self, routing=None):
        self._routing = routing or {}  # {session_key: project_name}

    def get_project(self, sk):
        return self._routing.get(sk)


class FakeProjectHandler:
    """Mock ProjectHandler — provides active project path."""

    def __init__(self, path=None):
        self._path = path


# ── Import test subject ──────────────────────────────────────────────────────────────────

from ui.handlers.agent_command_handler import AgentCommandHandler, _MAX_CHAIN_DEPTH


# ── Tests ────────────────────────────────────────────────────────────────────

class TestNoCommands:
    """Test that responses with no backtick commands are handled correctly."""

    def test_no_backtick_no_action(self):
        """Response with no backtick commands — no routing, no relay."""
        handler = AgentCommandHandler()
        fake_cmd = FakeCommandHandler()
        fake_rt = FakeAgentRuntimeHandler()
        handler.set_command_handler(fake_cmd)
        handler.set_agent_runtime_handler(fake_rt)

        handler.on_agent_response("special:coder", "Just a regular response", "crabwatch")

        assert len(fake_cmd.processed) == 0
        assert len(fake_rt.sent) == 0
        assert len(handler._pending_asks) == 0

    def test_empty_text_no_crash(self):
        """text='' and text=None — no action, no crash."""
        handler = AgentCommandHandler()
        fake_cmd = FakeCommandHandler()
        fake_rt = FakeAgentRuntimeHandler()
        handler.set_command_handler(fake_cmd)
        handler.set_agent_runtime_handler(fake_rt)

        # Should not raise
        handler.on_agent_response("special:coder", "", "crabwatch")
        handler.on_agent_response("special:coder", None, "crabwatch")

        assert len(fake_rt.sent) == 0

    def test_no_command_handler_set_no_crash(self):
        """CommandHandler is None — silent no-op."""
        handler = AgentCommandHandler()
        # No command handler set
        fake_rt = FakeAgentRuntimeHandler()
        handler.set_agent_runtime_handler(fake_rt)

        # Should not raise
        handler.on_agent_response("special:coder", "/ask @Debugger hello", "crabwatch")
        assert len(fake_rt.sent) == 0


class TestAskCommand:
    """Tests for /ask command — routing and pending-ask recording."""

    def test_ask_command_routes_to_special_agent(self):
        """/ask @Debugger question routes to Debugger via special agent send."""
        handler = AgentCommandHandler()
        fake_cmd = FakeCommandHandler(commands={"ask"})
        fake_rt = FakeAgentRuntimeHandler(special_agents={"special:debugger": "Debugger"})
        handler.set_command_handler(fake_cmd)
        handler.set_agent_runtime_handler(fake_rt)

        handler.on_agent_response(
            "special:coder",
            "/ask @Debugger \"should I use observer or polling?\"",
            "crabwatch"
        )

        assert len(fake_rt.sent) == 1
        target_sk, text = fake_rt.sent[0]
        assert target_sk == "special:debugger"
        assert "should I use observer" in text

    def test_ask_records_pending_ask(self):
        """Ask command records _pending_asks so relay can find the source."""
        handler = AgentCommandHandler()
        fake_cmd = FakeCommandHandler(commands={"ask"})
        fake_rt = FakeAgentRuntimeHandler(special_agents={"special:debugger": "Debugger"})
        handler.set_command_handler(fake_cmd)
        handler.set_agent_runtime_handler(fake_rt)

        handler.on_agent_response(
            "special:coder",
            "/ask @Debugger \"should I use X or Y?\"",
            "crabwatch"
        )

        assert "special:debugger" in handler._pending_asks
        assert handler._pending_asks["special:debugger"] == "special:coder"

    def test_ask_unknown_target_routes_via_runtime_receiver(self):
        """/ask @Qaster where Qaster is not a special agent → routed through the
        local runtime receiver. SPEC-05 R1 deleted the gateway branch; the
        receiver no-ops with a warning for unregistered/remote keys."""
        handler = AgentCommandHandler()
        fake_cmd = FakeCommandHandler(commands={"ask"})
        fake_rt = FakeAgentRuntimeHandler()  # no special agents
        fake_am = FakeAgentManager(names={"agent:qaster:...": "Qaster"})
        handler.set_command_handler(fake_cmd)
        handler.set_agent_runtime_handler(fake_rt)
        handler.set_agent_manager(fake_am)

        handler.on_agent_response(
            "special:coder",
            "/ask @Qaster \"is X compatible with the gateway?\"",
            "crabwatch"
        )

        # Routed via the local receiver — one send recorded
        assert len(fake_rt.sent) == 1


class TestTellCommand:
    """Tests for /tell command — routing without pending-ask."""

    def test_tell_command_routes_without_pending(self):
        """/tell @Agent info — message sent but no pending ask recorded."""
        handler = AgentCommandHandler()
        fake_cmd = FakeCommandHandler(commands={"ask", "tell"})
        fake_rt = FakeAgentRuntimeHandler(special_agents={"special:debugger": "Debugger"})
        handler.set_command_handler(fake_cmd)
        handler.set_agent_runtime_handler(fake_rt)

        handler.on_agent_response(
            "special:coder",
            "/tell @Debugger \"the feed.json has been cleared\"",
            "crabwatch"
        )

        assert len(fake_rt.sent) == 1
        # NO pending ask for tell — it's one-way
        assert "special:debugger" not in handler._pending_asks

    def test_delegate_command_records_pending(self):
        """/delegate @Agent task — like ask, records pending ask for relay."""
        handler = AgentCommandHandler()
        fake_cmd = FakeCommandHandler(commands={"ask", "delegate"})
        fake_rt = FakeAgentRuntimeHandler(special_agents={"special:debugger": "Debugger"})
        handler.set_command_handler(fake_cmd)
        handler.set_agent_runtime_handler(fake_rt)

        handler.on_agent_response(
            "special:coder",
            "/delegate @Debugger \"review the parser logic\"",
            "crabwatch"
        )

        assert len(fake_rt.sent) == 1
        assert "special:debugger" in handler._pending_asks
        assert handler._pending_asks["special:debugger"] == "special:coder"


class TestRelay:
    """Tests for relay mechanism — response delivered back to asking agent."""

    def test_relay_delivers_response_to_source(self):
        """When agent responds with a pending ask, relay to source agent."""
        handler = AgentCommandHandler()
        fake_rt = FakeAgentRuntimeHandler(special_agents={
            "special:debugger": "Debugger",
            "special:coder": "Coder",
        })
        handler.set_agent_runtime_handler(fake_rt)

        # Set up pending ask: Debugger was asked by Coder
        handler._pending_asks["special:debugger"] = "special:coder"

        handler.on_agent_response(
            "special:debugger",
            "Use the observer pattern. It integrates natively with watchdog.",
            "crabwatch"
        )

        # Relay should have been sent to Coder
        assert len(fake_rt.sent) == 1
        target_sk, relay_text = fake_rt.sent[0]
        assert target_sk == "special:coder"
        assert relay_text.startswith("[Debugger responded]:")
        assert "observer pattern" in relay_text

    def test_relay_clears_pending_ask(self):
        """After relay, the pending ask entry is removed."""
        handler = AgentCommandHandler()
        fake_rt = FakeAgentRuntimeHandler(special_agents={"special:debugger": "Debugger"})
        handler.set_agent_runtime_handler(fake_rt)

        handler._pending_asks["special:debugger"] = "special:coder"
        handler.on_agent_response("special:debugger", "Answer text", "crabwatch")

        assert "special:debugger" not in handler._pending_asks

    def test_relay_clears_source_chain_depth(self):
        """When relay is sent, source chain depth is cleared (relay is new context)."""
        handler = AgentCommandHandler()
        fake_rt = FakeAgentRuntimeHandler(special_agents={"special:debugger": "Debugger"})
        handler.set_agent_runtime_handler(fake_rt)

        handler._chain_depth["special:coder"] = 2
        handler._pending_asks["special:debugger"] = "special:coder"
        handler.on_agent_response("special:debugger", "Answer", "crabwatch")

        assert "special:coder" not in handler._chain_depth

    def test_no_pending_ask_no_relay(self):
        """Agent responds with no pending ask — no relay, only command scan."""
        handler = AgentCommandHandler()
        fake_rt = FakeAgentRuntimeHandler(special_agents={"special:debugger": "Debugger"})
        handler.set_agent_runtime_handler(fake_rt)

        # No pending ask — agent responds freely
        handler.on_agent_response("special:debugger", "Just answering without being asked", "crabwatch")

        assert len(fake_rt.sent) == 0

    def test_relay_message_format(self):
        """Relay text must start with '[AgentName responded]:'."""
        handler = AgentCommandHandler()
        fake_rt = FakeAgentRuntimeHandler(special_agents={
            "special:debugger": "Debugger",
            "special:coder": "Coder",
        })
        handler.set_agent_runtime_handler(fake_rt)

        handler._pending_asks["special:debugger"] = "special:coder"
        handler.on_agent_response("special:debugger", "Short answer.", "crabwatch")

        _, relay_text = fake_rt.sent[0]
        assert relay_text.startswith("[Debugger responded]:")

    def test_relay_nonspecial_target_to_special(self):
        """When the ask target is a non-special (agent-manager-registered) agent
        but the asker is a special agent, the response relays to the asker via
        the runtime receiver."""
        handler = AgentCommandHandler()
        fake_rt = FakeAgentRuntimeHandler(special_agents={"special:coder": "Coder"})
        fake_am = FakeAgentManager(names={"agent:qaster:...": "Qaster"})
        handler.set_agent_runtime_handler(fake_rt)
        handler.set_agent_manager(fake_am)

        # Qaster was asked by Coder
        handler._pending_asks["agent:qaster:..."] = "special:coder"
        handler.on_agent_response("agent:qaster:...", "Yes, X is compatible.", "crabwatch")

        # Relay goes to special agent via send_to_special_agent
        assert len(fake_rt.sent) == 1
        target_sk, relay_text = fake_rt.sent[0]
        assert target_sk == "special:coder"
        assert "[Qaster responded]:" in relay_text


class TestMultiHop:
    """Tests for multi-hop chains — relay + new command in same response."""

    def test_chain_depth_incremented_on_forward(self):
        """When agent A routes to agent B, B's chain depth = A's depth + 1."""
        handler = AgentCommandHandler()
        fake_cmd = FakeCommandHandler(commands={"ask"})
        fake_rt = FakeAgentRuntimeHandler(special_agents={"special:debugger": "Debugger"})
        fake_am = FakeAgentManager(names={"agent:qaster:...": "Qaster"})
        handler.set_command_handler(fake_cmd)
        handler.set_agent_runtime_handler(fake_rt)
        handler.set_agent_manager(fake_am)

        handler._chain_depth["special:coder"] = 0
        handler.on_agent_response(
            "special:coder",
            "/ask @Debugger \"is X compatible?\"",
            "crabwatch"
        )

        assert handler._chain_depth.get("special:debugger") == 1

    def test_chain_depth_limit_drops_commands(self):
        """When agent reaches chain depth >= _MAX_CHAIN_DEPTH, commands are dropped."""
        handler = AgentCommandHandler()
        fake_cmd = FakeCommandHandler(commands={"ask"})
        fake_rt = FakeAgentRuntimeHandler(special_agents={"special:debugger": "Debugger"})
        fake_am = FakeAgentManager(names={"agent:qaster:...": "Qaster"})
        handler.set_command_handler(fake_cmd)
        handler.set_agent_runtime_handler(fake_rt)
        handler.set_agent_manager(fake_am)

        # Simulate Coder at max depth
        handler._chain_depth["special:coder"] = _MAX_CHAIN_DEPTH

        handler.on_agent_response(
            "special:coder",
            "/ask @Debugger \"is X compatible?\"",
            "crabwatch"
        )

        # No routing — command dropped due to chain depth
        assert len(fake_rt.sent) == 0
        assert "special:debugger" not in handler._pending_asks

    def test_chain_depth_cleared_after_response(self):
        """After on_agent_response completes (no new commands), chain depth is cleared."""
        handler = AgentCommandHandler()
        fake_rt = FakeAgentRuntimeHandler(special_agents={"special:debugger": "Debugger"})
        handler.set_agent_runtime_handler(fake_rt)

        # Pre-set chain depth for the source agent
        handler._chain_depth["special:coder"] = 2
        # No pending ask, no commands → session_key is special:coder
        handler.on_agent_response("special:coder", "Just a regular response", "crabwatch")

        assert "special:coder" not in handler._chain_depth

    def test_no_pending_ask_just_chain_depth_cleared(self):
        """When no pending ask exists, chain depth still cleared on response."""
        handler = AgentCommandHandler()
        fake_rt = FakeAgentRuntimeHandler(special_agents={"special:debugger": "Debugger"})
        handler.set_agent_runtime_handler(fake_rt)

        # Pre-set chain depth for the responding agent
        handler._chain_depth["special:coder"] = 1
        handler.on_agent_response("special:coder", "No commands here", "crabwatch")

        assert "special:coder" not in handler._chain_depth


class TestFencedBlocks:
    """Tests for fenced code block stripping — avoid false positives."""

    def test_fenced_block_content_ignored(self):
        """Content inside fenced code blocks is not scanned for commands."""
        handler = AgentCommandHandler()
        fake_cmd = FakeCommandHandler(commands={"ask"})
        fake_rt = FakeAgentRuntimeHandler(special_agents={"special:debugger": "Debugger"})
        handler.set_command_handler(fake_cmd)
        handler.set_agent_runtime_handler(fake_rt)

        handler.on_agent_response(
            "special:coder",
            "Here's the fix:\n\n```python\nresult = /ask @Debugger  # not a command\n```\n\nBut /ask @Debugger \"is this right?\" ← IS a command",
            "crabwatch"
        )

        # Only the post-fence command should be routed
        assert len(fake_rt.sent) == 1
        assert fake_rt.sent[0][0] == "special:debugger"

    def test_non_command_backtick_ignored(self):
        """Single-backtick content that is NOT a known command is skipped."""
        handler = AgentCommandHandler()
        fake_cmd = FakeCommandHandler(commands={"ask"})  # "tell" is NOT registered
        fake_rt = FakeAgentRuntimeHandler(special_agents={"special:debugger": "Debugger"})
        handler.set_command_handler(fake_cmd)
        handler.set_agent_runtime_handler(fake_rt)

        handler.on_agent_response(
            "special:coder",
            "Try `print('hello') and see what happens",
            "crabwatch"
        )

        # print is not a command, no routing
        assert len(fake_rt.sent) == 0

    def test_multiple_backticks_in_fence_not_scanned(self):
        """Multiple backtick expressions inside fenced blocks are not scanned."""
        handler = AgentCommandHandler()
        fake_cmd = FakeCommandHandler(commands={"ask"})
        fake_rt = FakeAgentRuntimeHandler()
        handler.set_command_handler(fake_cmd)
        handler.set_agent_runtime_handler(fake_rt)

        handler.on_agent_response(
            "special:coder",
            "```\n/ask @Someone\n/ask @Else\n```",
            "crabwatch"
        )

        # No commands routed — fence content stripped
        assert len(fake_rt.sent) == 0


class TestMultipleCommands:
    """Tests for per-response command limit."""

    def test_multiple_commands_capped(self):
        """More than _MAX_COMMANDS_PER_RESPONSE commands — only first 3 processed."""
        handler = AgentCommandHandler()
        fake_cmd = FakeCommandHandler(commands={"ask"})
        fake_rt = FakeAgentRuntimeHandler(special_agents={
            "special:debugger": "Debugger",
            "special:coder": "Coder",
        })
        handler.set_command_handler(fake_cmd)
        handler.set_agent_runtime_handler(fake_rt)

        handler.on_agent_response(
            "special:coder",
            "/ask @Debugger \"q1\" /ask @Coder \"q2\" /ask @Debugger \"q3\" /ask @Coder \"q4\" /ask @Debugger \"q5\"",
            "crabwatch"
        )

        # Only first 3 commands processed
        assert len(fake_rt.sent) == 3

    def test_implicit_ask_via_at_mention(self):
        """Response starting with /@Agent is treated as implicit ask."""
        handler = AgentCommandHandler()
        # FakeCommandHandler processes backtick commands. When first_word.startswith("@"),
        # on_agent_response sets first_word="ask". process_input receives "/@Debugger ...".
        # FakeCommandHandler.process_input needs to handle the @debugger case.
        class AtAwareCommandHandler(FakeCommandHandler):
            def process_input(self, sk, text, skip_dispatch=False):
                if text.startswith("/@"):
                    # Treat as ask
                    rest = text[2:]  # strip leading backtick and @
                    parts = rest.split(" ", 1)
                    target = parts[0]
                    msg = parts[1] if len(parts) > 1 else ""
                    from models.command import CommandResult
                    return CommandResult(handled=True, forward_to=target, forward_text=msg)
                return super().process_input(sk, text, skip_dispatch=skip_dispatch)

        fake_rt = FakeAgentRuntimeHandler(special_agents={"special:debugger": "Debugger"})
        fake_cmd = AtAwareCommandHandler(commands={"ask"})
        handler.set_command_handler(fake_cmd)
        handler.set_agent_runtime_handler(fake_rt)

        handler.on_agent_response(
            "special:coder",
            "/@Debugger \"is this edge case valid?\"",
            "crabwatch"
        )

        assert len(fake_rt.sent) == 1
        assert fake_rt.sent[0][0] == "special:debugger"

    def test_unknown_command_ignored(self):
        """Backtick text starting with an unknown command is skipped."""
        handler = AgentCommandHandler()
        fake_cmd = FakeCommandHandler(commands={"ask"})  # "foobar" not registered
        fake_rt = FakeAgentRuntimeHandler(special_agents={"special:debugger": "Debugger"})
        handler.set_command_handler(fake_cmd)
        handler.set_agent_runtime_handler(fake_rt)

        handler.on_agent_response(
            "special:coder",
            "/foobar @Debugger some text",
            "crabwatch"
        )

        assert len(fake_rt.sent) == 0


class TestDisplayNameResolution:
    """Tests for display name resolution in relay prefix."""

    def test_relay_uses_special_agent_name(self):
        """Relay prefix uses special agent display name."""
        handler = AgentCommandHandler()
        fake_rt = FakeAgentRuntimeHandler(special_agents={
            "special:debugger": "DebuggEr",
            "special:coder": "Coder",
        })
        handler.set_agent_runtime_handler(fake_rt)
        handler._pending_asks["special:debugger"] = "special:coder"

        handler.on_agent_response("special:debugger", "Short answer.", "crabwatch")

        _, relay_text = fake_rt.sent[0]
        assert relay_text.startswith("[DebuggEr responded]:")

    def test_relay_uses_agent_manager_name(self):
        """Relay prefix falls back to AgentManager display name."""
        handler = AgentCommandHandler()
        fake_rt = FakeAgentRuntimeHandler(special_agents={"special:coder": "Coder"})
        fake_am = FakeAgentManager(names={"agent:qaster:...": "QasterBot"})
        handler.set_agent_runtime_handler(fake_rt)
        handler.set_agent_manager(fake_am)
        handler._pending_asks["agent:qaster:..."] = "special:coder"

        handler.on_agent_response("agent:qaster:...", "Answer.", "crabwatch")

        _, relay_text = fake_rt.sent[0]
        assert relay_text.startswith("[QasterBot responded]:")

    def test_relay_uses_session_key_fallback(self):
        """Relay prefix falls back to last segment of session key if no name found."""
        handler = AgentCommandHandler()
        fake_rt = FakeAgentRuntimeHandler(special_agents={"special:coder": "Coder"})
        handler.set_agent_runtime_handler(fake_rt)

        # Session key contains a "/" so split("/")[-1] produces a readable fallback name
        # Session key with an embedded "/" (remote-style key; ensures parsing handles it)
        target_sk = "agent/unknown/12345"
        handler._pending_asks[target_sk] = "special:coder"

        handler.on_agent_response(target_sk, "Answer.", "crabwatch")

        # source_sk="special:coder" is special → relay via send_to_special_agent
        assert len(fake_rt.sent) == 1
        _, relay_text = fake_rt.sent[0]
        # Fallback: session_key.split("/")[-1] = "12345"
        assert relay_text.startswith("[12345 responded]:")

# ═══════════════════════════════════════════════════════════════════
#  §7.2 — Missing agent-extractor tests
# ═══════════════════════════════════════════════════════════════════

class TestExtractorQuotedPayloads:
    """Tests for _extract_quoted_commands() — spec §7.2 edge cases."""

    def test_unquoted_payload_silently_skipped(self):
        """§7.2 #5: ask @QTR unquoted → 0 commands (no quotes)."""
        from ui.handlers.agent_command_handler import _extract_quoted_commands
        cmds = _extract_quoted_commands('/ask @QTR unquoted')
        assert len(cmds) == 0

    def test_no_space_before_quote_skipped(self):
        """§7.2 #7: ask @QTR"no space" → 0 commands (no space before quote)."""
        from ui.handlers.agent_command_handler import _extract_quoted_commands
        cmds = _extract_quoted_commands('/ask @QTR"no space"')
        assert len(cmds) == 0

    def test_stop_without_payload(self):
        """§7.2 #9: stop @QTR → 1 command with empty payload."""
        from ui.handlers.agent_command_handler import _extract_quoted_commands
        cmds = _extract_quoted_commands('/stop @QTR')
        assert len(cmds) == 1
        assert cmds[0].command == "stop"
        assert cmds[0].payload == ""

    def test_escaped_quotes_in_payload(self):
        """§7.2 #11: ask @QTR "she said \\"hi\\"" → payload = she said "hi"."""
        from ui.handlers.agent_command_handler import _extract_quoted_commands
        cmds = _extract_quoted_commands('/ask @QTR "she said \\"hi\\""')
        assert len(cmds) == 1
        assert cmds[0].payload == 'she said "hi"'

    def test_auto_close_unclosed_quote(self):
        """§4.4: opening quote found, no closing quote → auto-close."""
        from ui.handlers.agent_command_handler import _extract_quoted_commands
        cmds = _extract_quoted_commands('/ask @QTR "this is unclosed')
        assert len(cmds) == 1
        assert cmds[0].payload == "this is unclosed"

    def test_auto_close_empty_dropped(self):
        """§4.4: opening quote with nothing after → silently drop."""
        from ui.handlers.agent_command_handler import _extract_quoted_commands
        cmds = _extract_quoted_commands('/ask @QTR "')
        assert len(cmds) == 0

    def test_truncation_with_ellipsis(self):
        """§4.5: payload > 4096 chars → truncated with ellipsis marker."""
        from ui.handlers.agent_command_handler import _extract_quoted_commands
        big = "x" * 4100
        text = f'/ask @QTR "{big}"'
        cmds = _extract_quoted_commands(text)
        assert len(cmds) == 1
        assert len(cmds[0].payload) == 4097  # 4096 + 1 char ellipsis
        assert cmds[0].payload.endswith("…")

    def test_empty_payload_double_quote_skipped(self):
        """Spec 7.2 #6: empty quoted payload (two double quotes) is silently dropped."""
        from ui.handlers.agent_command_handler import _extract_quoted_commands
        cmds = _extract_quoted_commands('/ask @QTR ""')
        assert len(cmds) == 0

    def test_fenced_block_ignored(self):
        """§7.2 #10: command inside fenced block → 0 commands."""
        from ui.handlers.agent_command_handler import _extract_quoted_commands
        text = '```\nask @QTR "hi"`\n```'
        cmds = _extract_quoted_commands(text)
        assert len(cmds) == 0


class TestAuditReportProcessing:
    """SPEC-3 §7.3 — Structured audit report processing via on_agent_response()."""

    def test_audit_report_logged_to_review_log(self, tmp_path):
        """Structured audit report in agent message is written to review-log.jsonl."""
        from ui.handlers.agent_command_handler import AgentCommandHandler

        handler = AgentCommandHandler()
        handler.set_project_path_provider(lambda: str(tmp_path))

        # Build text without triple backticks (avoid quoting issues)
        lb = chr(10)
        text = (
            "## Audit Report" + lb +
            "**Task:** Test task" + lb +
            "**File:** test.py:10" + lb +
            "**Severity:** bug" + lb +
            "**Bug:** off by one error" + lb +
            "**Expected:** correct index" + lb +
            "**Actual:** wrong index" + lb +
            "**Pattern:** off-by-one" + lb
        )

        from unittest.mock import patch
        with patch("utils.feedback_processor.resolve_default_target_role", return_value="unknown"):
            handler.on_agent_response("session:qaster:123", text, "test-project")
        log = tmp_path / ".crabcakes" / "review-log.jsonl"
        assert log.exists()
        import json
        entries = [json.loads(line) for line in log.read_text().strip().split(lb)]
        assert len(entries) == 1
        assert entries[0]["bug"] == "off by one error"
        assert entries[0]["target_role"] == "unknown"

    def test_bug_severity_appended_to_journal(self, tmp_path, monkeypatch):
        """Bug-severity report is appended to {role}-bugs.md when structured_feedback=True."""
        from ui.handlers.agent_command_handler import AgentCommandHandler

        crab = tmp_path / ".crabcakes"
        crab.mkdir()
        lb = chr(10)
        journal_content = (
            "# Coder Bug Journal" + lb + lb +
            "---" + lb + lb +
            "## Bug #1 — 2026-05-17 — old.py" + lb + lb +
            "**Task:** old" + lb
        )
        (crab / "coder-bugs.md").write_text(journal_content)

        handler = AgentCommandHandler()
        handler.set_project_path_provider(lambda: str(tmp_path))

        # Monkeypatch load_agent_defs so feedback_processor sees structured_feedback=True
        def fake_defs():
            return [
                {
                    "role": "coder",
                    "tools": ["write_file"],
                    "self_improvement": {"structured_feedback": True},
                }
            ]

        monkeypatch.setattr("utils.agent_defs.load_agent_defs", fake_defs)
        text = (
            "## Audit Report" + lb +
            "**Task:** New task" + lb +
            "**File:** new.py:20" + lb +
            "**Severity:** bug" + lb +
            "**Bug:** new mistake" + lb +
            "**Expected:** correct" + lb +
            "**Actual:** wrong" + lb +
            "**Pattern:** test-pattern" + lb
        )

        handler.on_agent_response("session:qaster:123", text, "test-project")

        journal = crab / "coder-bugs.md"
        content = journal.read_text()
        assert "## Bug #2" in content
        assert "new.py" in content
        assert "test-pattern" in content

    def test_suggestion_severity_not_appended_to_journal(self, tmp_path, monkeypatch):
        """Suggestion-severity report is logged but NOT appended to bug journal."""
        from ui.handlers.agent_command_handler import AgentCommandHandler

        crab = tmp_path / ".crabcakes"
        crab.mkdir()
        handler = AgentCommandHandler()
        handler.set_project_path_provider(lambda: str(tmp_path))

        def fake_defs():
            return [
                {
                    "role": "coder",
                    "tools": ["write_file"],
                    "self_improvement": {"structured_feedback": True},
                }
            ]

        monkeypatch.setattr("utils.agent_defs.load_agent_defs", fake_defs)
        lb = chr(10)
        text = (
            "## Audit Report" + lb +
            "**Task:** Task" + lb +
            "**File:** file.py" + lb +
            "**Severity:** suggestion" + lb +
            "**Bug:** could be improved" + lb +
            "**Expected:** better" + lb +
            "**Actual:** current" + lb
        )

        handler.on_agent_response("session:qaster:123", text, "test-project")

        log = crab / "review-log.jsonl"
        assert log.exists()
        import json
        entry = json.loads(log.read_text().strip().split(lb)[0])
        assert entry["severity"] == "suggestion"
        journal = crab / "coder-bugs.md"
        assert not journal.exists()

    def test_no_project_path_skips_without_crash(self):
        """No project path provider set → reports detected but no crash."""
        from ui.handlers.agent_command_handler import AgentCommandHandler

        handler = AgentCommandHandler()
        # No project path provider set

        lb = chr(10)
        text = (
            "## Audit Report" + lb +
            "**Task:** Task" + lb +
            "**File:** file.py" + lb +
            "**Severity:** bug" + lb +
            "**Bug:** something wrong" + lb +
            "**Expected:** correct" + lb +
            "**Actual:** wrong" + lb
        )

        # Must not raise — graceful skip
        handler.on_agent_response("session:qaster:123", text, None)

    def test_audit_report_emits_feed_card_callback(self, tmp_path):
        """Feed card callback is fired with correct report data."""
        from ui.handlers.agent_command_handler import AgentCommandHandler

        handler = AgentCommandHandler()
        handler.set_project_path_provider(lambda: str(tmp_path))

        received = []
        handler.set_on_audit_report(lambda r: received.append(r))

        lb = chr(10)
        text = (
            "## Audit Report" + lb +
            "**Task:** Feed card test" + lb +
            "**File:** main.py:42" + lb +
            "**Severity:** bug" + lb +
            "**Bug:** null dereference" + lb +
            "**Expected:** safe access" + lb +
            "**Actual:** crash" + lb +
            "**Pattern:** null-check" + lb
        )

        from unittest.mock import patch
        with patch("utils.feedback_processor.resolve_default_target_role", return_value="unknown"):
            handler.on_agent_response("session:qaster:123", text, "test-project")

        assert len(received) == 1
        report = received[0]
        assert report["severity"] == "bug"
        assert report["file_path"] == "main.py:42"
        assert report["bug_description"] == "null dereference"
        assert report["pattern"] == "null-check"
        assert report["reviewer"] == "session:qaster:123"  # falls back to session key without AgentManager
        assert report["target_role"] == "unknown"

    def test_no_callback_set_still_works(self, tmp_path):
        """No crash when callback is not set."""
        from ui.handlers.agent_command_handler import AgentCommandHandler

        handler = AgentCommandHandler()
        handler.set_project_path_provider(lambda: str(tmp_path))
        # No set_on_audit_report called

        lb = chr(10)
        text = (
            "## Audit Report" + lb +
            "**Task:** No callback" + lb +
            "**File:** x.py:1" + lb +
            "**Severity:** suggestion" + lb +
            "**Bug:** style" + lb +
            "**Expected:** pretty" + lb +
            "**Actual:** ugly" + lb
        )

        handler.on_agent_response("session:qaster:123", text, "test-project")
        # If we get here without exception, test passes
        assert (tmp_path / ".crabcakes" / "review-log.jsonl").exists()


class TestAgentIssuedCompactClear:
    """Tests for agent-issued /compact and /clear commands."""

    def test_extract_compact_with_quoted_focus(self):
        """Parses /compact @Coder with quoted payload."""
        from ui.handlers.agent_command_handler import _extract_quoted_commands
        cmds = _extract_quoted_commands('text /compact @Coder "preserve auth" more')
        assert len(cmds) >= 1
        match = [c for c in cmds if c.command == "compact" and c.payload == "preserve auth"]
        assert len(match) == 1
        assert match[0].agent == "@Coder"

    def test_extract_clear_payload_free(self):
        """Parses /clear @Coder (payload-free via Pass 6)."""
        from ui.handlers.agent_command_handler import _extract_quoted_commands
        cmds = _extract_quoted_commands('text /clear @Coder more')
        assert len(cmds) >= 1
        match = [c for c in cmds if c.command == "clear"]
        assert len(match) == 1
        assert match[0].agent == "@Coder"
        assert match[0].payload == ""

    def test_extract_compact_payload_free(self):
        """Parses /compact @Coder (payload-free via Pass 6)."""
        from ui.handlers.agent_command_handler import _extract_quoted_commands
        cmds = _extract_quoted_commands('text /compact @Coder more')
        assert len(cmds) >= 1
        match = [c for c in cmds if c.command == "compact"]
        assert len(match) == 1
        assert match[0].agent == "@Coder"
        assert match[0].payload == ""

    def test_ask_still_works(self):
        """/ask still works alongside new commands."""
        from ui.handlers.agent_command_handler import _extract_quoted_commands
        cmds = _extract_quoted_commands('/ask @Coder "what is the status?"')
        assert len(cmds) >= 1
        match = [c for c in cmds if c.command == "ask"]
        assert len(match) == 1

    def test_on_agent_response_no_crash_when_runtime_unwired(self):
        """When agent_runtime_handler is None, /compact still works (action fires)
        but feedback injection is skipped gracefully."""
        from ui.handlers.agent_command_handler import AgentCommandHandler
        handler = AgentCommandHandler(GLib_module=None)
        handler.set_command_handler(None)  # no command handler = scanner skips
        # Should not raise
        handler.on_agent_response("special:supervisor", "no commands here", None)

    def test_no_duplicate_match_on_quoted_compact(self):
        """BUG #1: /compact @Coder "x" must produce exactly 1 command, not 2."""
        from ui.handlers.agent_command_handler import _extract_quoted_commands
        cmds = _extract_quoted_commands('/compact @Coder "preserve auth"')
        compact_cmds = [c for c in cmds if c.command == "compact"]
        assert len(compact_cmds) == 1, f"Duplicate match: {compact_cmds}"

    def test_clear_with_quoted_payload(self):
        """BUG #2: /clear @Coder "x" must parse (symmetric with compact)."""
        from ui.handlers.agent_command_handler import _extract_quoted_commands
        cmds = _extract_quoted_commands('/clear @Coder "session"')
        clear_cmds = [c for c in cmds if c.command == "clear"]
        assert len(clear_cmds) == 1, f"No clear command found: {cmds}"

    def test_record_action_result_calls_add_user_message(self):
        """BUG #4: _record_action_result must inject text into conversation."""
        from ui.handlers.agent_command_handler import AgentCommandHandler
        from unittest.mock import MagicMock

        handler = AgentCommandHandler(GLib_module=None)

        # Mock runtime handler chain
        mock_conv = MagicMock()
        mock_rt = MagicMock()
        mock_rt.get_conversation.return_value = mock_conv
        import threading
        mock_rt._lock = threading.Lock()

        mock_agent_def = MagicMock()
        mock_agent_def.display_name = "Supervisor"

        mock_arh = MagicMock()
        mock_arh.get_special_agent_def.return_value = mock_agent_def
        mock_arh._get_runtime.return_value = mock_rt

        handler._agent_runtime_handler = mock_arh

        handler._record_action_result("special:supervisor", "Compacted. Freed 12K tokens.")

        mock_conv.add_user_message.assert_called_once()
        call_arg = mock_conv.add_user_message.call_args[0][0]
        assert "[Action result]" in call_arg
        assert "Compacted" in call_arg

    def test_record_action_result_no_crash_when_agent_def_none(self):
        """When get_special_agent_def returns None, must not crash."""
        from ui.handlers.agent_command_handler import AgentCommandHandler
        from unittest.mock import MagicMock

        handler = AgentCommandHandler(GLib_module=None)
        mock_arh = MagicMock()
        mock_arh.get_special_agent_def.return_value = None
        handler._agent_runtime_handler = mock_arh

        handler._record_action_result("special:unknown", "test")
        # No exception = pass
