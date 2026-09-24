# tests/test_command_handler.py
# Unit tests for ui/handlers/command_handler.py.
#
# Philosophy: test the parsing and routing logic with fake collaborators
# (no GTK, no AgentManager — fake objects instead).
#
# Coverage:
#   1. Prefix detection (backtick vs wrong prefix, empty prefix, set_prefix)
#   2. Command lookup (known, unknown, alias, case-insensitive)
#   3. Flag parsing (value, no value, consecutive, followed by flag)
#   4. @mention resolution (exact, partial, empty, no match, multiple)
#   5. Body extraction (from quoted payload)
#   6. Error handling in handler → error response_text
#   7. set_agent_manager setter (gateway setter deleted, SPEC-05 SP2)
#   8. Command flow end-to-end
#   9. Internal _parse_flags and _parse_mentions unit tests


import pytest
import sys
sys.path.insert(0, '.')

from ui.handlers.command_handler import CommandHandler
from models.command import Command, CommandResult


# ═══════════════════════════════════════════════════════════════════
#  Fake Collaborators
# ═══════════════════════════════════════════════════════════════════

class FakeAgentManager:
    def __init__(self, names_to_keys: dict[str, str]):
        # names_to_keys: {name: session_key}
        self._name_to_key = dict(names_to_keys)
        self._key_to_name = {v: k for k, v in names_to_keys.items()}

    def get_names_ref(self) -> dict[str, str]:
        return dict(self._key_to_name)   # session_key → name

    def get_name(self, sk: str) -> str:
        return self._key_to_name.get(sk, "")


class FakeProjectHandler:
    def __init__(self, active_proj: str = "testproj", members: list[str] | None = None):
        self._active = active_proj
        self._members = members or ["agent:a:1", "agent:b:2"]

    def get_active_project_name(self) -> str | None:
        return self._active

    def get_project_members(self, proj: str) -> list[str]:
        return self._members


# ═══════════════════════════════════════════════════════════════════
#  Fixtures
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture
def empty_handler():
    return CommandHandler(
        agent_manager=None,
        project_handler=None,
        GLib_module=None,
    )


@pytest.fixture
def configured_handler():
    """Handler with AgentManager + ProjectHandler wired, plus echo command."""
    agnt = FakeAgentManager({
        "Debugger": "agent:debugger:1",
        "Coder": "agent:coder:2",
        "Qat": "agent:qat:3",
        "QDebug": "agent:qdebug:4",   # second agent with "q" — for @q partial-match test
    })
    proj = FakeProjectHandler("testproj", ["agent:a:1", "agent:b:2"])
    h = CommandHandler(
        agent_manager=agnt,
        project_handler=proj,
        GLib_module=None,
    )
    def echo(cmd: Command) -> CommandResult:
        return CommandResult(handled=True, response_text=f"echo: {cmd.name}")
    h.register_command("echo", echo, aliases=["e"], help_text="Echo test")
    return h


# ═══════════════════════════════════════════════════════════════════
#  Prefix detection
# ═══════════════════════════════════════════════════════════════════

class TestPrefixDetection:
    def test_plain_text_not_a_command(self, empty_handler):
        result = empty_handler.process_input("agent:1", "hello world")
        assert result.handled is False

    def test_wrong_prefix_not_a_command(self, empty_handler):
        result = empty_handler.process_input("agent:1", "/echo hello")
        assert result.handled is False

    def test_only_backtick_prefix_not_command(self, empty_handler):
        result = empty_handler.process_input("agent:1", "`")
        assert result.handled is False

    def test_only_backtick_whitespace_not_command(self, empty_handler):
        result = empty_handler.process_input("agent:1", "`   ")
        assert result.handled is False

    def test_set_prefix_changes_detection(self, empty_handler):
        empty_handler.set_prefix("/")
        from models.command import CommandResult
        empty_handler.register_command("echo", lambda c: CommandResult(handled=True, response_text="ok"))
        result = empty_handler.process_input("agent:1", "/echo hello")
        assert result.handled is True


# ═══════════════════════════════════════════════════════════════════
#  Command lookup
# ═══════════════════════════════════════════════════════════════════

class TestCommandLookup:
    def test_unknown_command_passes_through(self, configured_handler):
        result = configured_handler.process_input("agent:1", "/unknowncmd arg")
        assert result.handled is False

    def test_known_command_handled(self, configured_handler):
        result = configured_handler.process_input("agent:1", "/echo @Debugger \"hello\"")
        assert result.handled is True

    def test_alias_resolves(self, configured_handler):
        result = configured_handler.process_input("agent:1", "/e hello")
        assert result.handled is True

    def test_case_insensitive(self, configured_handler):
        result = configured_handler.process_input("agent:1", "/ECHO hello")
        assert result.handled is True


# ═══════════════════════════════════════════════════════════════════
#  Flag parsing
# ═══════════════════════════════════════════════════════════════════

class TestFlagParsing:
    def test_no_flags(self, configured_handler):
        result = configured_handler.process_input("agent:1", "/echo hi")
        assert result.handled is True

    def test_flag_no_value(self, configured_handler):
        result = configured_handler.process_input("agent:1", "/echo --verbose")
        assert result.handled is True

    def test_flag_with_value(self, configured_handler):
        result = configured_handler.process_input("agent:1", "/echo --verbose true")
        assert result.handled is True

    def test_multiple_flags(self, configured_handler):
        result = configured_handler.process_input("agent:1", "/echo --verbose true --detail high")
        assert result.handled is True


# ═══════════════════════════════════════════════════════════════════
#  Body extraction
# ═══════════════════════════════════════════════════════════════════

class TestBodyExtraction:
    def test_body_extracted(self, configured_handler):
        def capture(cmd: Command) -> CommandResult:
            return CommandResult(handled=True, response_text=cmd.body)
        configured_handler.register_command("bodytest", capture)
        result = configured_handler.process_input("agent:1", "/bodytest @Debugger \"actual body text\"")
        assert result.handled is True
        assert "actual body text" in result.response_text


# ═══════════════════════════════════════════════════════════════════
#  @mention resolution
# ═══════════════════════════════════════════════════════════════════

class TestMentionResolution:
    def test_exact_name_resolves(self, configured_handler):

        result = configured_handler.process_input("agent:1", "/echo @Debugger \"hi\"")
        assert result.handled is True

    def test_partial_name_resolves(self, configured_handler):

        result = configured_handler.process_input("agent:1", "/echo @debug \"hi\"")
        assert result.handled is True

    def test_empty_mention_no_project(self):
        """Empty @ with no project handler → error response_text."""
        agnt = FakeAgentManager({})
        h = CommandHandler(
            agent_manager=agnt,
            project_handler=None, GLib_module=None,
        )
        h.register_command("stop", lambda c: CommandResult(handled=True, response_text="ok"))
        result = h.process_input("agent:1", "/stop @")
        assert result.handled is True
        assert "No active project" in result.response_text

    def test_unknown_mention_returns_error(self, configured_handler):


        result = configured_handler.process_input("agent:1", '/echo @Nobody "hi"')
        assert "Unknown agent" in result.response_text
    def test_multiple_partial_matches_returns_error(self):
        """Two agents sharing a prefix → error."""
        agnt = FakeAgentManager({
            "DebugA": "agent:da:1",
            "DebugB": "agent:db:2",
        })
        h = CommandHandler(
            agent_manager=agnt,
            project_handler=None, GLib_module=None,
        )
        h.register_command("echo", lambda c: CommandResult(handled=True, response_text="ok"))
        result = h.process_input("agent:1", '/echo @deb "hi"')
        assert result.handled is True
        assert "Multiple agents" in result.response_text


# ═══════════════════════════════════════════════════════════════════
#  Error handling
# ═══════════════════════════════════════════════════════════════════

class TestErrorHandling:
    def test_handler_exception_returns_error_response(self, configured_handler):
        def bad(cmd: Command) -> CommandResult:
            raise RuntimeError("boom")
        configured_handler.register_command("bad", bad)

        result = configured_handler.process_input("agent:1", '/bad @Debugger "test"')
        assert result.handled is True
        assert "Error" in result.response_text
        assert "boom" in result.response_text


# ═══════════════════════════════════════════════════════════════════
#  Setters
# ═══════════════════════════════════════════════════════════════════

class TestSetters:
    def test_set_agent_manager(self, empty_handler):
        class FakeAM:
            pass
        am = FakeAM()
        empty_handler.set_agent_manager(am)
        assert empty_handler._agent_mgr is am

    def test_set_special_agents(self, empty_handler):
        agents = {"special:coder": "Coder", "special:debugger": "Debugger"}
        empty_handler.set_special_agents(agents)
        assert empty_handler._special_agents == agents


# ═══════════════════════════════════════════════════════════════════
#  Command flow end-to-end
# ═══════════════════════════════════════════════════════════════════

class TestCommandFlow:
    def test_response_text_sets_no_forward_to(self, configured_handler):
        result = configured_handler.process_input("agent:1", "/echo @Debugger \"hello\"")
        assert result.handled is True
        assert result.response_text == "echo: echo"
        assert result.forward_to is None


# ═══════════════════════════════════════════════════════════════════
#  _parse_flags internal unit
# ═══════════════════════════════════════════════════════════════════

class TestParseFlagsInternal:
    def test_flag_consumes_value(self, empty_handler):
        flags, rest = empty_handler._parse_flags(["--verbose", "true", "arg"])
        assert flags == {"verbose": "true"}
        assert rest == ["arg"]

    def test_flag_without_value(self, empty_handler):
        # Design: --flag greedily takes the next non-flag token as its value
        flags, rest = empty_handler._parse_flags(["--verbose", "arg"])
        assert flags == {"verbose": "arg"}
        assert rest == []

    def test_consecutive_flags(self, empty_handler):
        flags, rest = empty_handler._parse_flags(["--a", "1", "--b", "2"])
        assert flags == {"a": "1", "b": "2"}
        assert rest == []

    def test_flag_followed_by_flag(self, empty_handler):
        # --verbose --detail high: --verbose has no value, --detail takes "high"
        flags, rest = empty_handler._parse_flags(["--verbose", "--detail", "high"])
        assert flags == {"verbose": "", "detail": "high"}
        assert rest == []

    def test_no_flags(self, empty_handler):
        flags, rest = empty_handler._parse_flags(["arg1", "arg2"])
        assert flags == {}
        assert rest == ["arg1", "arg2"]


# ═══════════════════════════════════════════════════════════════════
#  _parse_mentions internal unit
# ═══════════════════════════════════════════════════════════════════

class TestParseMentionsInternal:
    def test_single_mention(self, empty_handler):
        mentions, rest = empty_handler._parse_mentions(["@debugger", "arg1"])
        assert mentions == ["@debugger"]
        assert rest == ["arg1"]

    def test_multiple_consecutive_mentions(self, empty_handler):
        mentions, rest = empty_handler._parse_mentions(["@a", "@b", "arg1"])
        assert mentions == ["@a", "@b"]
        assert rest == ["arg1"]

    def test_non_mention_breaks_mention_run(self, empty_handler):
        # Non-@ token ends the mention run; subsequent @ become regular args
        mentions, rest = empty_handler._parse_mentions(["@a", "stop", "@b"])
        assert mentions == ["@a"]
        assert rest == ["stop", "@b"]

    def test_no_mentions(self, empty_handler):
        mentions, rest = empty_handler._parse_mentions(["arg1", "arg2"])
        assert mentions == []
        assert rest == ["arg1", "arg2"]

    def test_empty_list(self, empty_handler):
        mentions, rest = empty_handler._parse_mentions([])
        assert mentions == []
        assert rest == []


# ═══════════════════════════════════════════════════════════════════
#  resolve_inline_mention (plain-text @ routing, no backtick)
# ═══════════════════════════════════════════════════════════════════

class TestResolveInlineMention:
    """Tests for CommandHandler.resolve_inline_mention() — the public API
    used by ChatHandler for plain-text @ routing in project tabs."""

    def test_no_mention_returns_empty_resolution(self, configured_handler):
        r = configured_handler.resolve_inline_mention("hello world", "project:testproj")
        assert r.target_session_key is None
        assert not r.is_broadcast
        assert r.clean_text == "hello world"
        assert r.error is None

    def test_single_agent_resolved(self, configured_handler):
        r = configured_handler.resolve_inline_mention("@Debugger fix this", "project:testproj")
        assert r.target_session_key == "agent:debugger:1"
        assert r.clean_text == "fix this"
        assert not r.is_broadcast
        assert r.error is None

    def test_mid_text_mention_resolved(self, configured_handler):
        r = configured_handler.resolve_inline_mention("hello @Debugger fix this", "project:testproj")
        assert r.target_session_key == "agent:debugger:1"
        assert r.clean_text == "hello fix this"
        assert r.error is None

    def test_broadcast_resolved(self, configured_handler):
        r = configured_handler.resolve_inline_mention("@ hello team", "project:testproj")
        assert r.is_broadcast
        assert len(r.broadcast_targets) == 2  # from FakeProjectHandler
        assert r.clean_text == "hello team"

    def test_unknown_agent_error(self, configured_handler):
        r = configured_handler.resolve_inline_mention("@Nobody hello", "project:testproj")
        assert r.error is not None
        assert "Unknown" in r.error

    def test_multiple_mentions_error(self, configured_handler):
        r = configured_handler.resolve_inline_mention("@Debugger @Coder hello", "project:testproj")
        assert r.error is not None
        assert "Only one" in r.error

    def test_empty_text_returns_empty(self, configured_handler):
        r = configured_handler.resolve_inline_mention("", "project:testproj")
        assert r.target_session_key is None
        assert r.clean_text == ""

    def test_non_string_returns_empty(self, configured_handler):
        r = configured_handler.resolve_inline_mention(123, "project:testproj")
        assert r.target_session_key is None


# ═══════════════════════════════════════════════════════════════════
#  Special Agent @mention resolution (Phase A2A Step 1.4)
# ═══════════════════════════════════════════════════════════════════

class TestSpecialAgentMentionResolution:
    """Tests for @mention resolution of special agents (Coder, Debugger).
    These live in AgentRuntimeHandler, not AgentManager — verified via
    set_special_agents() registry in CommandHandler."""

    def test_exact_special_agent_resolves(self):
        """@Coder → special:coder via special agents registry."""
        agnt = FakeAgentManager({"QTR": "agent:qtr:telegram:direct:7478874934"})
        h = CommandHandler(agent_manager=agnt, project_handler=None)
        h.set_special_agents({"special:coder": "Coder", "special:debugger": "Debugger"})
        resolved = h._resolve_mention("@Coder")
        assert resolved == "special:coder"

    def test_exact_debugger_resolves(self):
        """@Debugger → special:debugger via special agents registry."""
        agnt = FakeAgentManager({})
        h = CommandHandler(agent_manager=agnt, project_handler=None)
        h.set_special_agents({"special:coder": "Coder", "special:debugger": "Debugger"})
        resolved = h._resolve_mention("@Debugger")
        assert resolved == "special:debugger"

    def test_prefix_match_special_agent(self):
        """@Co → special:coder via prefix match (min 2 chars)."""
        agnt = FakeAgentManager({})
        h = CommandHandler(agent_manager=agnt, project_handler=None)
        h.set_special_agents({"special:coder": "Coder", "special:debugger": "Debugger"})
        resolved = h._resolve_mention("@Co")
        assert resolved == "special:coder"

    def test_single_char_no_partial_match(self):
        """@C (single char) → no prefix match, falls through to unknown."""
        agnt = FakeAgentManager({})
        h = CommandHandler(agent_manager=agnt, project_handler=None)
        h.set_special_agents({"special:coder": "Coder", "special:debugger": "Debugger"})
        resolved = h._resolve_mention("@C")
        assert isinstance(resolved, CommandResult)
        assert "Unknown" in resolved.response_text

    def test_unknown_special_agent_returns_error(self):
        """@NotAnAgent → error via special agents registry."""
        agnt = FakeAgentManager({})
        h = CommandHandler(agent_manager=agnt, project_handler=None)
        h.set_special_agents({"special:coder": "Coder", "special:debugger": "Debugger"})
        resolved = h._resolve_mention("@NotAnAgent")
        assert isinstance(resolved, CommandResult)
        assert "Unknown" in resolved.response_text

    def test_agentmgr_and_special_coexist_no_collision(self):
        """Agent-manager-registered agent and special agent share a name — the
        agent-manager name wins (checked first in _resolve_mention). MVP note:
        agent_manager is always None post-R1 (accepted wiring gap), so this
        precedence is currently inert but pinned."""
        # Agent-manager-registered agent named "Coder" (unusual but possible)
        agnt = FakeAgentManager({"Coder": "agent:coder:gateway:1"})
        h = CommandHandler(agent_manager=agnt, project_handler=None)
        # Special agent registry also has Coder
        h.set_special_agents({"special:coder": "Coder"})
        # _resolve_mention checks agent-manager names BEFORE the special registry
        # This test documents current behavior: agent-mgr wins (checked first)
        resolved = h._resolve_mention("@Coder")
        # Agent-mgr name checked first → returns its session key
        assert resolved == "agent:coder:gateway:1"

    def test_mixed_resolve_via_resolve_inline_mention(self):
        """resolve_inline_mention works for both agent-manager-registered and special agents."""
        agnt = FakeAgentManager({"QTR": "agent:qtr:telegram:direct:7478874934"})
        h = CommandHandler(agent_manager=agnt, project_handler=None)
        h.set_special_agents({"special:coder": "Coder", "special:debugger": "Debugger"})

        # Agent-manager-registered agent
        r1 = h.resolve_inline_mention("@QTR hello", "project:testproj")
        assert r1.target_session_key == "agent:qtr:telegram:direct:7478874934"

        # Special agent
        r2 = h.resolve_inline_mention("@Coder hello", "project:testproj")
        assert r2.target_session_key == "special:coder"

    def test_resolve_inline_mention_debugger(self):
        """resolve_inline_mention for @Debugger → special:debugger."""
        agnt = FakeAgentManager({})
        h = CommandHandler(agent_manager=agnt, project_handler=None)
        h.set_special_agents({"special:debugger": "Debugger"})
        r = h.resolve_inline_mention("@Debugger fix this", "project:testproj")
        assert r.target_session_key == "special:debugger"
        assert r.clean_text == "fix this"

class TestBugFixes:
    """Regression tests for bugs found during adversarial audit."""

    def test_bug1_bare_at_mention_implicit_ask(self, configured_handler):
        """Bug #1: `@Qaster hello treated @Qaster as command name → broadcast.
        Fix: first token starting with @ triggers implicit 'ask' command."""
        def fake_ask(cmd: Command) -> CommandResult:
            return CommandResult(
                handled=True,
                forward_to=cmd.target_session_key,
                forward_text=cmd.body,
            )
        configured_handler.register_command("ask", fake_ask)
        result = configured_handler.process_input("agent:1", "/@Debugger \"hello\"")
        assert result.handled is True
        assert result.forward_to == "agent:debugger:1"
        assert result.forward_text == "hello"

    def test_bug2_no_emdash_args_become_body(self, configured_handler):
        """Bug #2: `ask @Debugger hello (no em-dash) → body was empty.
        Fix: args after @mention stripping become body when body is empty."""
        def capture(cmd: Command) -> CommandResult:
            return CommandResult(
                handled=True,
                forward_to=cmd.target_session_key,
                forward_text=cmd.body,
            )
        configured_handler.register_command("capture", capture)
        result = configured_handler.process_input("agent:1", "/capture @Debugger \"hello world\"")
        assert result.handled is True
        assert result.forward_text == "hello world"

    def test_bug3_multiple_mentions_rejected(self, configured_handler):
        """Bug #3: multiple @mentions silently dropped.
        Fix: explicit error when >1 mention found."""
        configured_handler.register_command("ask", lambda c: CommandResult(handled=True))
        result = configured_handler.process_input("agent:1", '/ask @Debugger @Coder "hello"')
        assert result.handled is True
        assert "Only one" in result.response_text

    def test_bug5_prefix_matching_not_contains(self):
        """Bug #5: @a matched every agent with 'a' in name.
        Fix: use startswith instead of contains, min 2 chars for partial."""
        agnt = FakeAgentManager({
            "Alpha": "agent:a:1",
            "Beta": "agent:b:2",
            "Gamma": "agent:g:3",
        })
        h = CommandHandler(agent_manager=agnt, project_handler=None)
        # 1-char query: exact match only (no partial), so @a with no agent named "a" → unknown
        resolved = h._resolve_mention("@a")
        assert isinstance(resolved, CommandResult)
        assert "Unknown" in resolved.response_text
        # Exact match still works
        resolved = h._resolve_mention("@Alpha")
        assert isinstance(resolved, str) and resolved == "agent:a:1"
        # 2-char prefix match works
        resolved = h._resolve_mention("@al")
        assert isinstance(resolved, str) and resolved == "agent:a:1"
        # @x has no match
        resolved = h._resolve_mention("@xy")
        assert isinstance(resolved, CommandResult)
        assert "Unknown" in resolved.response_text

    def test_bug6_broadcast_uses_session_key_project(self):
        """Bug #6: @ broadcast used global active project, not tab context.
        Fix: _resolve_mention uses session_key to extract project name."""
        agnt = FakeAgentManager({"A": "agent:a:1"})

        class MultiProjectHandler:
            def __init__(self):
                self._active = "wrong-project"
            def get_active_project_name(self):
                return self._active
            def get_project_members(self, p):
                if p == "right-project":
                    return ["agent:a:1"]
                return ["agent:other:99"]

        ph = MultiProjectHandler()
        h = CommandHandler(agent_manager=agnt, project_handler=ph)
        # @ broadcast from project:right-project tab should use right-project
        resolved = h._resolve_mention("@", session_key="project:right-project")
        assert isinstance(resolved, list)
        assert resolved == ["agent:a:1"]  # right-project members, not wrong-project


# ═══════════════════════════════════════════════════════════════════
#  Work Handler registration (SPEC-TASK-SYSTEM-FULL-REDESIGN §5.1)
# ═══════════════════════════════════════════════════════════════════

class TestWorkHandlerRegistration:
    """Verifies /work + legacy names are registered as SEPARATE canonical
    commands (no aliases=), each routing to its WorkHandler method, all
    payload_free=True, and /work is canonical in help output."""

    def _make_handler(self):
        class FakeWorkHandler:
            def cmd_work(self, cmd):
                return CommandResult(handled=True, response_text=f"work:{cmd.name}")
            def cmd_work_list(self, cmd):
                return CommandResult(handled=True, response_text=f"list:{cmd.name}")
            def cmd_work_start(self, cmd):
                return CommandResult(handled=True, response_text=f"start:{cmd.name}")
            def cmd_work_done(self, cmd):
                return CommandResult(handled=True, response_text=f"done:{cmd.name}")
            def cmd_work_blocked(self, cmd):
                return CommandResult(handled=True, response_text=f"blocked:{cmd.name}")
            def cmd_work_cancel(self, cmd):
                return CommandResult(handled=True, response_text=f"cancel:{cmd.name}")
            def cmd_work_assign(self, cmd):
                return CommandResult(handled=True, response_text=f"assign:{cmd.name}")
            def cmd_work_priority(self, cmd):
                return CommandResult(handled=True, response_text=f"priority:{cmd.name}")

        wh = FakeWorkHandler()
        h = CommandHandler(
            agent_manager=None,
            project_handler=None,
            GLib_module=None,
            work_handler=wh,
        )
        return h, wh

    def test_work_resolves_to_cmd_work(self):
        """Canonical /work routes to cmd_work and appears canonical in help."""
        h, wh = self._make_handler()
        res = h.process_input("agent:1", "/work \"My title\"")
        assert res.handled is True
        assert res.response_text == "work:work"

    def test_task_routes_to_cmd_work_no_orphan(self):
        """Legacy /task maps to cmd_work (NOT a separate cmd_task); both /work
        and /task resolve — no collision / no orphan."""
        h, wh = self._make_handler()
        res_task = h.process_input("agent:1", "/task \"Something\"")
        assert res_task.handled is True
        assert res_task.response_text == "work:task"
        # /work still resolves after /task registered (the §5.1 invariant)
        res_work = h.process_input("agent:1", "/work \"Something\"")
        assert res_work.handled is True
        assert res_work.response_text == "work:work"

    def test_legacy_names_route_to_methods(self):
        """Each legacy name routes to its corresponding cmd_work_* method."""
        cases = {
            "/start 00000001": "start:start",
            "/done 00000001": "done:done",
            "/blocked 00000001": "blocked:blocked",
            "/cancel 00000001": "cancel:cancel",
            "/assign 00000001": "assign:assign",
            "/priority 00000001": "priority:priority",
        }
        h, wh = self._make_handler()
        for text, expected in cases.items():
            res = h.process_input("agent:1", text)
            assert res.handled is True, text
            assert res.response_text == expected, text

    def test_tasks_routes_to_cmd_work_list(self):
        """/tasks (plural) maps to cmd_work_list."""
        h, wh = self._make_handler()
        res = h.process_input("agent:1", "/tasks")
        assert res.handled is True
        assert res.response_text == "list:tasks"

    def test_all_work_commands_payload_free(self):
        """All 9 work command names are registered payload_free=True."""
        h, wh = self._make_handler()
        for name in ("work", "task", "tasks", "start", "done",
                     "blocked", "cancel", "assign", "priority"):
            assert h._registry.is_payload_free(name), name

    def test_work_canonical_in_help_output(self):
        """/work appears in the command list and has help text."""
        h, wh = self._make_handler()
        help_res = h.process_input("agent:1", "/help")
        assert "work" in help_res.response_text
        work_help = h.process_input("agent:1", "/help work")
        assert "Work units" in work_help.response_text

    def test_no_alias_for_work_commands(self):
        """The registration block must not use aliases= for any work command."""
        h, wh = self._make_handler()
        # All 9 names must be canonical commands (in _commands), NOT aliases.
        aliases = h._registry.list_aliases()
        for name in ("work", "task", "tasks", "start", "done",
                     "blocked", "cancel", "assign", "priority"):
            assert name not in aliases, f"{name} is registered as an alias, must be canonical"
            assert name in h._registry.list_commands(), f"{name} is not a canonical command"

    def test_all_9_work_names_in_commands_dict_not_alias_resolved(self):
        """Spec §5.1 no-aliases invariant — direct check against the canonical
        _commands dict. list_commands() returns the union of canonical names
        and alias-target names, so it cannot distinguish a canonical /task
        from /work registered with aliases=['task']. The _commands dict holds
        ONLY canonical entries; checking it directly proves each of the 9
        names is registered as its own canonical command."""
        h, _wh = self._make_handler()
        canonical = h._registry._commands  # private but stable internal dict
        for name in ("work", "task", "tasks", "start", "done",
                     "blocked", "cancel", "assign", "priority"):
            assert name in canonical, (
                f"{name!r} missing from canonical _commands dict — likely "
                f"registered via aliases= which violates spec §5.1"
            )


# ═══════════════════════════════════════════════════════════════════
#  §7.3 — Missing integration tests (A2A_QUOTED_PAYLOAD_SPEC)
# ═══════════════════════════════════════════════════════════════════

class TestQuotedPayloadIntegration:
    """Integration tests for quoted-payload commands through process_input()."""

    def test_malformed_unquoted_error(self, configured_handler):
        """§7.3 #2: `ask @QTR hello → error about quoted payload."""
        configured_handler.register_command("ask", lambda c: CommandResult(handled=True))
        result = configured_handler.process_input("agent:1", "/ask @Debugger hello")
        assert result.handled is True
        assert "Malformed command" in result.response_text
        assert "payload must be quoted" in result.response_text

    def test_unclosed_quote_error(self, configured_handler):
        """§7.3 #5: `ask @QTR "unclosed → error about unclosed quote."""
        configured_handler.register_command("ask", lambda c: CommandResult(handled=True))
        result = configured_handler.process_input("agent:1", '/ask @Debugger "unclosed')
        assert result.handled is True
        assert "Unclosed quote" in result.response_text

    def test_empty_payload_error(self, configured_handler):
        """§7.3 #3: `ask @QTR "" → error about empty payload."""
        configured_handler.register_command("ask", lambda c: CommandResult(handled=True))
        result = configured_handler.process_input("agent:1", '/ask @Debugger ""')
        assert result.handled is True
        assert "Empty payload" in result.response_text

    def test_stop_no_payload_required(self, configured_handler):
        """§7.3 #6: `stop @QTR → handled, no error about missing payload."""
        configured_handler.register_command("stop", lambda c: CommandResult(handled=True, response_text="ok"),
            payload_free=True)
        result = configured_handler.process_input("agent:1", "/stop @Debugger")
        assert result.handled is True
        assert "Malformed" not in result.response_text

    def test_clear_no_payload_required(self, configured_handler):
        """`/clear → handled, no error about missing payload.
        Bug: _PAYLOAD_FREE frozenset was missing 'clear', so /clear returned
        'Malformed command — payload must be quoted' instead of clearing the
        conversation. Structural fix: payload_free is now a register_command()
        parameter checked via registry, not a hardcoded set."""
        configured_handler.register_command("clear", lambda c: CommandResult(handled=True, response_text="Cleared."),
            payload_free=True)
        result = configured_handler.process_input("special:supervisor", "/clear")
        assert result.handled is True
        assert result.response_text == "Cleared."
        assert "Malformed" not in result.response_text

    def test_clear_without_payload_free_flag_fails(self, configured_handler):
        """Regression guard: if register_command is called WITHOUT payload_free=True
        for clear, the command must fail with Malformed — proving the test above
        exercises the real payload_free mechanism, not a pass-through."""
        configured_handler.register_command("clear", lambda c: CommandResult(handled=True, response_text="Cleared."))
        result = configured_handler.process_input("special:supervisor", "/clear")
        assert result.handled is True
        assert "Malformed" in result.response_text

    def test_quoted_payload_body_passed_to_handler(self, configured_handler):
        """Payload extracted correctly and passed as cmd.body."""
        captured_body = []
        def capture(cmd: Command) -> CommandResult:
            captured_body.append(cmd.body)
            return CommandResult(handled=True, response_text="ok")
        configured_handler.register_command("ask", capture)
        result = configured_handler.process_input("agent:1", '/ask @Debugger "what about edge cases?"')
        assert result.handled is True
        assert captured_body[0] == "what about edge cases?"

    def test_escaped_quotes_in_body(self, configured_handler):
        """Escaped quotes in payload become literal quotes in cmd.body."""
        captured_body = []
        def capture(cmd: Command) -> CommandResult:
            captured_body.append(cmd.body)
            return CommandResult(handled=True, response_text="ok")
        configured_handler.register_command("ask", capture)
        result = configured_handler.process_input("agent:1", '/ask @Debugger "she said \\"hello\\""')
        assert result.handled is True
        assert captured_body[0] == 'she said "hello"'

    def test_4k_cap_on_user_input(self, configured_handler):
        """§4.5: user payloads over 4K are truncated with ellipsis."""
        captured_body = []
        def capture(cmd: Command) -> CommandResult:
            captured_body.append(cmd.body)
            return CommandResult(handled=True, response_text="ok")
        configured_handler.register_command("ask", capture)
        big = "x" * 5000
        result = configured_handler.process_input("agent:1", f'/ask @Debugger "{big}"')
        assert result.handled is True
        assert len(captured_body[0]) == 4097  # 4096 + 1 char ellipsis
        assert captured_body[0].endswith("…")
