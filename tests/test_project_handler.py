# tests/test_project_handler.py
# Tests for ui/handlers/project_handler.py — ProjectHandler.
#
# Principle: test the failure modes that would break callers.
# Mock MainContent, LeftPanel, projects module at the boundary.
# Do NOT mock internal state.

import pytest
from unittest.mock import MagicMock


class FakeGLib:
    """Simulates GLib.idle_add — stores callbacks but does NOT run them."""

    def __init__(self):
        self._pending = []

    def idle_add(self, fn, *args, **kwargs):
        self._pending.append((fn, args, kwargs))
        return len(self._pending)

    def dispatch_all(self):
        """Run all pending callbacks — simulates GTK main loop."""
        results = []
        while self._pending:
            fn, args, kwargs = self._pending.pop(0)
            result = fn(*args, **kwargs)
            results.append(result)
        return results


class FakeProjects:
    def __init__(self):
        self._members = {}

    def load_members(self, project_name):
        return self._members.get(project_name, [])

    def save_members(self, project_name, members):
        self._members[project_name] = list(members)

    def load_projects(self):
        return []


@pytest.fixture
def fake_glib():
    return FakeGLib()


@pytest.fixture
def fake_projects():
    return FakeProjects()


@pytest.fixture
def mc():
    return MagicMock(name="main_content")


@pytest.fixture
def lp():
    return MagicMock(name="left_panel")


@pytest.fixture
def handler(lp, fake_projects, fake_glib):
    from ui.handlers.project_handler import ProjectHandler
    from models import AgentRoutingTable
    agent_to_project = AgentRoutingTable()
    return ProjectHandler(
        left_panel=lp,
        projects_module=fake_projects,
        agent_to_project=agent_to_project,
        GLib_module=fake_glib,
    )


# ── open_project ─────────────────────────────────────────────────────────────

class TestOpenProject:
    def test_refreshes_agents_list(self, handler, lp, fake_glib):
        handler.open_project("my-project", "/path/to/my-project")
        fake_glib.dispatch_all()
        lp.refresh_agents_with_project.assert_called_once_with("my-project")

    def test_populates_agent_to_project_lookup(self, handler, fake_projects):
        fake_projects.save_members("my-project", ["agent:a", "agent:b"])
        handler.open_project("my-project", "/path/to/my-project")
        assert handler.get_project_for_agent("agent:a") == "my-project"
        assert handler.get_project_for_agent("agent:b") == "my-project"

    def test_calls_on_project_opened_callback(self, handler, fake_glib):
        cb = MagicMock()
        handler.set_on_project_opened(cb)
        handler.open_project("my-project", "/path/to/my-project")
        fake_glib.dispatch_all()
        cb.assert_called_once_with("my-project", "/path/to/my-project")


# ── toggle_agent ─────────────────────────────────────────────────────────────

class TestToggleAgent:
    def test_adds_new_agent(self, handler, fake_projects):
        fake_projects.save_members("proj", ["agent:1"])
        handler.open_project("proj", "/p")
        handler.toggle_agent("agent:2")
        assert fake_projects.load_members("proj") == ["agent:1", "agent:2"]

    def test_removes_existing_agent(self, handler, fake_projects):
        fake_projects.save_members("proj", ["agent:1", "agent:2"])
        handler.open_project("proj", "/p")
        handler.toggle_agent("agent:1")
        assert fake_projects.load_members("proj") == ["agent:2"]

    def test_refreshes_agents_list_after_toggle(self, handler, lp, fake_projects, fake_glib):
        fake_projects.save_members("proj", ["agent:1"])
        handler.open_project("proj", "/p")
        lp.reset_mock()
        handler.toggle_agent("agent:2")
        fake_glib.dispatch_all()
        lp.refresh_agents_with_project.assert_called_with("proj")

    def test_calls_on_members_changed_callback(self, handler, fake_projects, fake_glib):
        fake_projects.save_members("proj", ["agent:1"])
        handler.open_project("proj", "/p")
        cb = MagicMock()
        handler.set_on_members_changed(cb)
        handler.toggle_agent("agent:2")
        fake_glib.dispatch_all()
        cb.assert_called_once_with("proj", ["agent:1", "agent:2"])

    def test_noop_when_no_active_project(self, handler, fake_projects):
        handler.toggle_agent("agent:1")  # no project open
        # no crash — just a no-op


# ── Routing API ───────────────────────────────────────────────────────────────

class TestRoutingApi:
    def test_is_project_session_true_for_member(self, handler, fake_projects):
        fake_projects.save_members("proj", ["agent:a"])
        handler.open_project("proj", "/p")
        assert handler.is_project_session("agent:a") is True

    def test_is_project_session_false_for_unknown(self, handler):
        assert handler.is_project_session("agent:unknown") is False

    def test_get_project_for_agent_returns_correct_project(self, handler, fake_projects):
        fake_projects.save_members("proj-a", ["agent:x"])
        fake_projects.save_members("proj-b", ["agent:y"])
        handler.open_project("proj-a", "/a")
        handler.open_project("proj-b", "/b")
        assert handler.get_project_for_agent("agent:x") == "proj-a"
        assert handler.get_project_for_agent("agent:y") == "proj-b"

    def test_get_project_members_returns_list(self, handler, fake_projects):
        fake_projects.save_members("proj", ["a", "b", "c"])
        handler.open_project("proj", "/p")
        members = handler.get_project_members("proj")
        assert set(members) == {"a", "b", "c"}

    def test_get_active_project_name_returns_none_initially(self, handler):
        assert handler.get_active_project_name() is None

    def test_get_active_project_name_returns_name_after_open(self, handler):
        handler.open_project("my-proj", "/p")
        assert handler.get_active_project_name() == "my-proj"


# ── Session Switching ────────────────────────────────────────────────────────

class FakeAgentManager:
    """Minimal AgentManager mock for session switching tests."""
    def __init__(self, sessions_by_name=None):
        # sessions_by_name: {agent_name: [session_keys]}
        self._sessions = sessions_by_name or {}
        self._names = {}  # session_key -> name
        for name, keys in self._sessions.items():
            for sk in keys:
                self._names[sk] = name

    def get_sessions(self, agent_name):
        return self._sessions.get(agent_name, [])

    def get_name(self, session_key):
        return self._names.get(session_key, "")


class TestGetAgentSessionInProject:
    def test_returns_matching_session(self, handler, fake_projects):
        fake_projects.save_members("proj", ["agent:qaster:main"])
        handler.open_project("proj", "/p")
        handler.set_agent_manager(FakeAgentManager(
            sessions_by_name={"qaster": ["agent:qaster:main", "agent:qaster:telegram:123"]}
        ))
        result = handler.get_agent_session_in_project("proj", "qaster")
        assert result == "agent:qaster:main"

    def test_returns_none_if_agent_not_member(self, handler, fake_projects):
        fake_projects.save_members("proj", ["agent:other:main"])
        handler.open_project("proj", "/p")
        handler.set_agent_manager(FakeAgentManager(
            sessions_by_name={"qaster": ["agent:qaster:main"]}
        ))
        result = handler.get_agent_session_in_project("proj", "qaster")
        assert result is None

    def test_returns_none_if_no_agent_manager(self, handler, fake_projects):
        fake_projects.save_members("proj", ["agent:qaster:main"])
        handler.open_project("proj", "/p")
        # agent_mgr not set
        result = handler.get_agent_session_in_project("proj", "qaster")
        assert result is None


class TestUpdateAgentSession:
    def test_replaces_session_in_members(self, handler, fake_projects):
        fake_projects.save_members("proj", ["agent:qaster:main", "agent:qrusher:main"])
        handler.open_project("proj", "/p")
        handler.update_agent_session("proj", "agent:qaster:main", "agent:qaster:telegram:123")
        members = fake_projects.load_members("proj")
        assert "agent:qaster:main" not in members
        assert "agent:qaster:telegram:123" in members
        assert "agent:qrusher:main" in members

    def test_updates_routing_table(self, handler, fake_projects):
        fake_projects.save_members("proj", ["agent:qaster:main"])
        handler.open_project("proj", "/p")
        handler.update_agent_session("proj", "agent:qaster:main", "agent:qaster:telegram:123")
        assert handler.is_project_session("agent:qaster:telegram:123")
        assert not handler.is_project_session("agent:qaster:main")

    def test_migrates_solo_target(self, handler, fake_projects):
        fake_projects.save_members("proj", ["agent:qaster:main"])
        handler.open_project("proj", "/p")
        handler.set_solo_target("proj", "agent:qaster:main")
        handler.update_agent_session("proj", "agent:qaster:main", "agent:qaster:telegram:123")
        assert handler.get_solo_target("proj") == "agent:qaster:telegram:123"

    def test_noop_if_old_key_not_member(self, handler, fake_projects):
        fake_projects.save_members("proj", ["agent:other:main"])
        handler.open_project("proj", "/p")
        handler.update_agent_session("proj", "agent:qaster:main", "agent:qaster:telegram:123")
        # No change
        members = fake_projects.load_members("proj")
        assert members == ["agent:other:main"]


# ── /status command (SPEC-AUDIT-CLEANUP-2 Phase 2a — work_store migration) ──

def _make_status_cmd(session_key: str = "project:myproj"):
    """Build a /status command issued from the given session key."""
    from models.command import Command
    return Command(name="status", source_session_key=session_key)


class TestCmdStatus:
    """Spec: docs/specs/SPEC-AUDIT-CLEANUP-2-DEAD-CODE-SWEEP.md §Phase 2a.

    After SPEC-49 (work-system removal) /status reports Project / Members /
    Solo DM (plus provider-failure lines when present) — it no longer
    counts work units, and after SPEC-58 H1 it has no Review line.
    """

    def test_non_project_session_returns_hint(self, handler, fake_projects):
        """Guard: /status outside a project tab returns the hint message."""
        result = handler.cmd_status(_make_status_cmd("agent:coder"))
        assert result.handled is True
        assert "Open a project tab" in result.response_text

    def test_other_lines_byte_identical(self, handler, fake_projects):
        """The non-count lines keep their exact pre-removal format."""
        fake_projects.save_members("myproj", ["agent:coder"])
        handler.open_project("myproj", "/p")
        handler.set_solo_target("myproj", "agent:coder")
        result = handler.cmd_status(_make_status_cmd())
        lines = result.response_text.splitlines()
        assert lines[0] == "Project: myproj"
        assert lines[1] == "Members: 1"
        assert lines[2] == "Solo DM: @coder"
        assert not any(ln.startswith("Work units:") for ln in lines)
        assert not any(ln.startswith("Review:") for ln in lines)


# ── revert_file_to_sha (SPEC-58 H1 — standalone revert) ─────────────────────

class TestRevertFileToSha:
    """SPEC-58 H1: ProjectHandler.revert_file_to_sha is standalone — no review
    session and no ReviewHandler; failures surface via set_on_revert_status."""

    @staticmethod
    def _make_git_result(success=True, error=""):
        from utils.git_ops import GitResult
        return GitResult(
            success=success,
            stdout="",
            error=error if not success else "",
            sha="abc123" if success else None,
        )

    @staticmethod
    def _run_worker_sync(monkeypatch):
        """Patch threading.Thread so the daemon worker runs synchronously."""
        class _SyncThread:
            def __init__(self, target=None, args=(), kwargs=None, daemon=False):
                self._target = target
                self._args = args
                self._kwargs = kwargs or {}

            def start(self):
                self._target(*self._args, **self._kwargs)

        monkeypatch.setattr("threading.Thread", _SyncThread)

    def _patch_checkout(self, monkeypatch, result):
        calls = []
        monkeypatch.setattr(
            "ui.handlers.project_handler.checkout_paths",
            lambda *a, **k: calls.append((a, k)) or result,
        )
        return calls

    def test_noop_when_file_path_not_str(self, handler, fake_projects, monkeypatch):
        fake_projects.save_members("proj", ["agent:x"])
        handler.open_project("proj", "/p")
        calls = self._patch_checkout(monkeypatch, self._make_git_result())
        handler.revert_file_to_sha("proj", ["file.py"], "abc123")
        assert calls == []

    def test_noop_when_file_path_starts_with_dash(self, handler, fake_projects, monkeypatch):
        fake_projects.save_members("proj", ["agent:x"])
        handler.open_project("proj", "/p")
        calls = self._patch_checkout(monkeypatch, self._make_git_result())
        handler.revert_file_to_sha("proj", "--output=file.py", "abc123")
        assert calls == []

    def test_noop_when_project_path_unresolved(self, handler, monkeypatch):
        # No open project → _get_project_path returns None → no checkout.
        calls = self._patch_checkout(monkeypatch, self._make_git_result())
        handler.revert_file_to_sha("nope", "file.py", "abc123")
        assert calls == []

    def test_success_checkout_and_on_complete(self, handler, fake_projects, fake_glib, monkeypatch):
        fake_projects.save_members("proj", ["agent:x"])
        handler.open_project("proj", "/p")
        self._run_worker_sync(monkeypatch)
        calls = self._patch_checkout(monkeypatch, self._make_git_result(success=True))
        completed = MagicMock()
        handler.revert_file_to_sha(
            "proj", "file.py", "abc123", on_complete=completed
        )
        fake_glib.dispatch_all()
        # checkout_paths(project_path, sha, [file_path])
        assert len(calls) == 1
        args, kwargs = calls[0]
        assert args[0] == "/p"          # resolved project path
        assert args[1] == "abc123"      # target sha
        assert args[2] == ["file.py"]   # single-file path list
        completed.assert_called_once_with()

    def test_failure_logs_warning_and_dispatches_toast(
        self, handler, fake_projects, fake_glib, monkeypatch
    ):
        fake_projects.save_members("proj", ["agent:x"])
        handler.open_project("proj", "/p")
        self._run_worker_sync(monkeypatch)
        self._patch_checkout(
            monkeypatch,
            self._make_git_result(success=False, error="boom"),
        )
        status_cb = MagicMock()
        handler.set_on_revert_status(status_cb)
        completed = MagicMock()
        handler.revert_file_to_sha(
            "proj", "file.py", "badsha", on_complete=completed
        )
        fake_glib.dispatch_all()
        completed.assert_not_called()
        status_cb.assert_called_once_with(
            "project:proj", "⚠ Failed to revert file.py: boom"
        )

    def test_no_review_dependency(self, handler, fake_projects, fake_glib, monkeypatch):
        # Never wired a review handler — revert still runs checkout.
        fake_projects.save_members("proj", ["agent:x"])
        handler.open_project("proj", "/p")
        self._run_worker_sync(monkeypatch)
        calls = self._patch_checkout(monkeypatch, self._make_git_result(success=True))
        completed = MagicMock()
        handler.revert_file_to_sha(
            "proj", "file.py", "abc123", on_complete=completed
        )
        fake_glib.dispatch_all()
        assert len(calls) == 1
        completed.assert_called_once_with()


# ── /cost command (Phase 2 — token tracking) ────────────────────────────────

class TestCmdCost:
    """Spec: docs/specs/SPEC-token-tracking-fix.md AC-1/2/3/5.

    cmd_cost reads each member's (tokens, cost) from a persisted
    conversation file (agent:*) or the injected in-memory cache
    (agent:*) and formats a table. Falls back to (0, 0.0) when neither
    source is available.
    """

    def _make_cmd(self, project_name: str):
        from models.command import Command
        return Command(name="cost", source_session_key=f"project:{project_name}")

    def test_reads_total_tokens_and_cost_from_conversation_file(
        self, handler, fake_projects, tmp_path, monkeypatch
    ):
        """AC-1/AC-2: special agent conversation file with real usage data."""
        import json
        import os
        from utils.config import get_config_dir

        # Arrange: configure to use tmp config dir, write a conversation file
        config_dir = tmp_path / "crabcakes"
        (config_dir / "conversations").mkdir(parents=True)
        monkeypatch.setattr(
            "utils.config.get_config_dir", lambda: str(config_dir)
        )
        # Also patch where the handler imports from (deferred import inside the
        # function pulls the live `get_config_dir` symbol each call).
        monkeypatch.setattr(
            "ui.handlers.project_handler.get_config_dir", lambda: str(config_dir),
            raising=False,
        )

        # Write a realistic conversation file
        conv_data = {
            "session_key": "agent:coder",
            "agent_name": "coder",
            "total_tokens": 5000,
            "total_cost": 0.15,
            "step_count": 3,
            "messages": [],
            "system_prompt": "",
        }
        (config_dir / "conversations" / "agent:coder.json").write_text(
            json.dumps(conv_data)
        )

        # Project has one special member
        fake_projects.save_members("myproj", ["agent:coder"])
        handler.open_project("myproj", "/p")

        # Act
        result = handler.cmd_cost(self._make_cmd("myproj"))

        # Assert: real numbers, not zeros
        assert result.handled is True
        text = result.response_text
        assert "5,000 tokens" in text, f"expected 5,000 tokens in output, got: {text!r}"
        assert "$0.1500" in text, f"expected $0.1500 in output, got: {text!r}"
        # Old stub text must be gone
        assert "contact gateway" not in text
        assert "usage API" not in text

    def test_missing_conversation_file_shows_zeros(
        self, handler, fake_projects, tmp_path, monkeypatch
    ):
        """AC-3: no file, no cache → 0 tokens / $0.0000."""
        from utils.config import get_config_dir
        config_dir = tmp_path / "crabcakes"
        (config_dir / "conversations").mkdir(parents=True)
        monkeypatch.setattr(
            "utils.config.get_config_dir", lambda: str(config_dir)
        )
        monkeypatch.setattr(
            "ui.handlers.project_handler.get_config_dir", lambda: str(config_dir),
            raising=False,
        )

        # No conversation file written. _runtime_usage_fn is None (default).
        fake_projects.save_members("myproj", ["agent:coder"])
        handler.open_project("myproj", "/p")

        result = handler.cmd_cost(self._make_cmd("myproj"))

        assert result.handled is True
        text = result.response_text
        assert "0 tokens" in text, f"expected 0 tokens in output, got: {text!r}"
        assert "$0.0000" in text, f"expected $0.0000 in output, got: {text!r}"
        assert "contact gateway" not in text

    def test_corrupted_conversation_file_falls_back_to_zeros(
        self, handler, fake_projects, tmp_path, monkeypatch
    ):
        """Sad path: malformed JSON must NOT crash — return (0, 0.0)."""
        from utils.config import get_config_dir
        config_dir = tmp_path / "crabcakes"
        (config_dir / "conversations").mkdir(parents=True)
        monkeypatch.setattr(
            "utils.config.get_config_dir", lambda: str(config_dir)
        )
        monkeypatch.setattr(
            "ui.handlers.project_handler.get_config_dir", lambda: str(config_dir),
            raising=False,
        )

        # Write a corrupted conversation file
        (config_dir / "conversations" / "agent:coder.json").write_text(
            "{this is not valid json"
        )

        fake_projects.save_members("myproj", ["agent:coder"])
        handler.open_project("myproj", "/p")

        # Must not raise
        result = handler.cmd_cost(self._make_cmd("myproj"))

        assert result.handled is True
        text = result.response_text
        assert "0 tokens" in text, f"expected graceful 0 tokens fallback, got: {text!r}"
        assert "$0.0000" in text, f"expected graceful $0.0000 fallback, got: {text!r}"

    def test_in_memory_cache_used_for_gateway_agents(
        self, handler, fake_projects
    ):
        """AC-5: gateway agent (agent:*) uses injected in-memory cache."""
        # Gateway agent — no conversation file path
        fake_projects.save_members("myproj", ["agent:qtr:telegram:1"])
        handler.open_project("myproj", "/p")

        # Wire the runtime usage callback with cached data
        handler.set_runtime_usage_fn(
            lambda: {"agent:qtr:telegram:1": (12345, 0.0234)}
        )

        result = handler.cmd_cost(self._make_cmd("myproj"))

        assert result.handled is True
        text = result.response_text
        assert "12,345 tokens" in text, f"expected 12,345 tokens, got: {text!r}"
        assert "$0.0234" in text, f"expected $0.0234, got: {text!r}"

    def test_runtime_usage_fn_exception_falls_back_gracefully(
        self, handler, fake_projects
    ):
        """Sad path: misbehaving callback must not crash cmd_cost."""
        fake_projects.save_members("myproj", ["agent:qtr:telegram:1"])
        handler.open_project("myproj", "/p")

        def boom():
            raise RuntimeError("runtime handler exploded")
        handler.set_runtime_usage_fn(boom)

        # Must not raise
        result = handler.cmd_cost(self._make_cmd("myproj"))
        assert result.handled is True
        # No file + broken cache → zeros
        assert "0 tokens" in result.response_text
        assert "$0.0000" in result.response_text

    def test_non_project_session_returns_hint(self, handler, fake_projects):
        """Guard: /cost outside a project tab returns the hint message."""
        from models.command import Command
        fake_projects.save_members("myproj", ["agent:coder"])
        handler.open_project("myproj", "/p")

        # Simulate /cost in an agent tab (not a project tab)
        cmd = Command(name="cost", source_session_key="agent:coder")
        result = handler.cmd_cost(cmd)
        assert result.handled is True
        assert "Open a project tab" in result.response_text

# ═══════════════════════════════════════════════════════════════════
#  /clear UI side-effect (clear-ui-fix.md)
# ═══════════════════════════════════════════════════════════════════
#
# Regression tests for the bug where /clear reset the data plane
# (messages, step_count, tokens, cost) but left the visible chat box
# full of stale bubbles. The fix adds a clear_chat callback that
# empties the chat box after the data-plane clear succeeds.

from unittest.mock import MagicMock, call
from models.command import Command


def _make_clear_cmd(session_key: str) -> Command:
    """Build a /clear command targeting the given session_key."""
    return Command(name="clear", source_session_key=session_key)


class TestCmdClearChatSideEffect:
    """Tests A and B: /clear invokes the clear_chat callback exactly once
    for agent: session_keys, AFTER the data-plane clear succeeds. If
    the clear_chat callback raises, the data-plane clear is not rolled
    back (the user still gets the success response_text).
    """

    def test_cmd_clear_invokes_clear_chat_callback(self, handler, lp, fake_projects, fake_glib):
        """Test A: after cmd_clear runs for a agent: session, the
        clear_chat callback was invoked once with the session key.
        """
        from models.command import CommandResult

        # Wire the data-plane clear (returns True = success)
        handler.set_clear_callback(lambda sk: True)
        # Wire the UI side-effect clear with a spy
        clear_chat_spy = MagicMock()
        handler.set_clear_chat_callback(clear_chat_spy)

        result = handler.cmd_clear(_make_clear_cmd("agent:coder"))

        # Data-plane clear returned True → cmd_clear returned success
        assert result.handled is True
        assert "Cleared coder's conversation" in result.response_text
        # UI side effect ran exactly once with the session key
        clear_chat_spy.assert_called_once_with("agent:coder")

    def test_cmd_clear_does_not_invoke_clear_chat_on_data_plane_failure(self, handler, fake_projects, fake_glib):
        """If the data-plane clear returns False, the UI side effect
        must NOT run — the conversation wasn't actually cleared, so
        emptying the chat box would create state-view divergence.
        """
        # Data-plane clear returns False
        handler.set_clear_callback(lambda sk: False)
        clear_chat_spy = MagicMock()
        handler.set_clear_chat_callback(clear_chat_spy)

        result = handler.cmd_clear(_make_clear_cmd("agent:coder"))

        assert result.handled is True
        assert "Could not clear" in result.response_text
        # UI side effect did NOT run
        clear_chat_spy.assert_not_called()

    def test_cmd_clear_does_not_invoke_clear_chat_on_data_plane_exception(self, handler, fake_projects, fake_glib):
        """If the data-plane clear RAISES, cmd_clear returns the
        'Clear failed for X' error and the UI side effect must NOT run.
        """
        def boom(_sk):
            raise RuntimeError("data plane exploded")
        handler.set_clear_callback(boom)
        clear_chat_spy = MagicMock()
        handler.set_clear_chat_callback(clear_chat_spy)

        result = handler.cmd_clear(_make_clear_cmd("agent:coder"))

        assert result.handled is True
        assert "Clear failed for coder" in result.response_text
        clear_chat_spy.assert_not_called()

    def test_cmd_clear_swallows_clear_chat_exception(self, handler, fake_projects, fake_glib):
        """Test B: if the clear_chat callback raises, cmd_clear still
        returns handled=True with a success message — the data-plane
        reset already succeeded and must not be rolled back.
        """
        def clear_chat_boom(_sk):
            raise RuntimeError("GTK exploded")
        handler.set_clear_callback(lambda sk: True)
        handler.set_clear_chat_callback(clear_chat_boom)

        result = handler.cmd_clear(_make_clear_cmd("agent:coder"))

        # The user still gets the success message — the data-plane clear
        # already happened, and a UI failure must not block the response.
        assert result.handled is True
        assert "Cleared coder's conversation" in result.response_text

    def test_cmd_clear_does_not_invoke_clear_chat_for_project_tabs(self, handler, fake_projects, fake_glib):
        """/clear in a project tab returns the 'use in an agent tab' hint
        and must not run either callback (the data plane isn't even called
        for project tabs because each member has their own conversation).
        """
        clear_data_spy = MagicMock()
        clear_chat_spy = MagicMock()
        handler.set_clear_callback(clear_data_spy)
        handler.set_clear_chat_callback(clear_chat_spy)

        result = handler.cmd_clear(_make_clear_cmd("project:crabcakes"))

        assert result.handled is True
        assert "Use /clear in an agent tab" in result.response_text
        clear_data_spy.assert_not_called()
        clear_chat_spy.assert_not_called()

    def test_cmd_clear_does_not_invoke_clear_chat_for_unknown_prefix(self, handler, fake_projects, fake_glib):
        """/clear with a session_key prefix we don't recognize returns
        a 'Cannot clear session' hint and runs neither callback.
        """
        clear_data_spy = MagicMock()
        clear_chat_spy = MagicMock()
        handler.set_clear_callback(clear_data_spy)
        handler.set_clear_chat_callback(clear_chat_spy)

        result = handler.cmd_clear(_make_clear_cmd("foo:bar"))

        assert result.handled is True
        assert "Cannot clear session" in result.response_text
        clear_data_spy.assert_not_called()
        clear_chat_spy.assert_not_called()


class TestSetClearChatCallback:
    """Direct test of the setter contract: set_clear_chat_callback
    stores the callback so cmd_clear can invoke it later.
    """

    def test_setter_stores_callback(self, handler):
        cb = MagicMock()
        handler.set_clear_chat_callback(cb)
        assert handler._clear_chat_callback is cb

    def test_setter_accepts_none_to_unregister(self, handler):
        handler.set_clear_chat_callback(MagicMock())
        handler.set_clear_chat_callback(None)
        assert handler._clear_chat_callback is None


# ═══════════════════════════════════════════════════════════════════
#  SOR §2.6 — ProjectHandler: auto-add metadata, save_members backfill,
#  created callback. Uses a REAL utils.project_awareness against tmp_path
#  (matching tests/test_create_project.py) for the awareness fixture.
# ═══════════════════════════════════════════════════════════════════

def _pa_handler(projects_mod, fake_glib):
    """Build a ProjectHandler with real utils.project_awareness."""
    from ui.handlers.project_handler import ProjectHandler
    from models import AgentRoutingTable
    import utils.project_awareness as pa
    return ProjectHandler(
        left_panel=MagicMock(name="left_panel"),
        projects_module=projects_mod,
        agent_to_project=AgentRoutingTable(),
        GLib_module=fake_glib,
        awareness_module=pa,
    )


class TestAutoAddOnboardingAgentsMetadata:
    def test_uses_agent_def_role_and_can_write(self, tmp_path, monkeypatch):
        """_auto_add_onboarding_agents uses the def's role/can_write, NOT the
        hardcoded 'onboarding guide'/True."""
        from agent.special_agents import SpecialAgentDef
        import utils.project_awareness as pa

        project_path = str(tmp_path)
        pa.init_project_config(project_path, "demo")

        def _fake_onboarding():
            return [SpecialAgentDef(
                conv_id_prefix="agent:supervisor",
                display_name="Supervisor",
                role="supervisor",
                emoji="🧭",
                tools=["read_file", "write_file", "edit_file"],
                can_write=True,
            )]
        monkeypatch.setattr(
            "agent.special_agents.get_project_onboarding_agents", _fake_onboarding
        )

        projects_mod = MagicMock()
        projects_mod._PROJECTS_DIR_REF = [str(tmp_path)]
        projects_mod.load_projects.return_value = []
        ph = _pa_handler(projects_mod, FakeGLib())

        ph._auto_add_onboarding_agents(project_path)

        team = pa.load_team(project_path)
        member = team.get_member("agent:supervisor")
        assert member is not None
        assert member.name == "Supervisor"
        assert member.role == "supervisor"
        assert member.can_write is True


class TestSaveMembersBackfill:
    def _open_active(self, ph, name, path):
        ph._active_project_name = name
        ph._active_project_path = path

    def test_backfills_special_metadata(self, tmp_path, monkeypatch):
        """_save_members resolves agent:* metadata via get_special_agent."""
        from agent.special_agents import SpecialAgentDef
        import utils.project_awareness as pa

        project_path = str(tmp_path)
        pa.init_project_config(project_path, "proj")

        def _fake_get(prefix):
            if prefix == "agent:supervisor":
                return SpecialAgentDef(
                    conv_id_prefix="agent:supervisor",
                    display_name="Supervisor",
                    role="supervisor",
                    emoji="🧭",
                    tools=["read_file", "write_file"],
                    can_write=True,
                )
            return None
        monkeypatch.setattr("agent.special_agents.get_special_agent", _fake_get)

        projects_mod = MagicMock()
        projects_mod._PROJECTS_DIR_REF = [str(tmp_path)]
        ph = _pa_handler(projects_mod, FakeGLib())
        self._open_active(ph, "proj", project_path)

        ph._save_members("proj", ["agent:supervisor"])

        team = pa.load_team(project_path)
        member = team.get_member("agent:supervisor")
        assert member is not None
        assert member.name == "Supervisor"
        assert member.role == "supervisor"
        assert member.can_write is True

    def test_gateway_key_stays_blank(self, tmp_path, monkeypatch):
        """_save_members leaves gateway agent: keys blank (display-time resolution)."""
        import utils.project_awareness as pa

        project_path = str(tmp_path)
        pa.init_project_config(project_path, "proj")

        monkeypatch.setattr("agent.special_agents.get_special_agent", lambda _: None)

        projects_mod = MagicMock()
        projects_mod._PROJECTS_DIR_REF = [str(tmp_path)]
        ph = _pa_handler(projects_mod, FakeGLib())
        self._open_active(ph, "proj", project_path)

        ph._save_members("proj", ["agent:qtr:telegram:direct:123"])

        team = pa.load_team(project_path)
        member = team.get_member("agent:qtr:telegram:direct:123")
        assert member is not None
        assert member.name == ""
        assert member.role == ""
        assert member.can_write is False

    def test_preserves_existing_member(self, tmp_path):
        """_save_members preserves an existing member's metadata exactly."""
        from models.team import ProjectTeam, TeamMember
        import utils.project_awareness as pa

        project_path = str(tmp_path)
        pa.init_project_config(project_path, "proj")
        pa.save_team(project_path, ProjectTeam(members=[
            TeamMember(session_key="agent:supervisor", name="OldName", role="custom", can_write=False),
        ]))

        projects_mod = MagicMock()
        projects_mod._PROJECTS_DIR_REF = [str(tmp_path)]
        ph = _pa_handler(projects_mod, FakeGLib())
        self._open_active(ph, "proj", project_path)

        ph._save_members("proj", ["agent:supervisor"])

        team = pa.load_team(project_path)
        member = team.get_member("agent:supervisor")
        assert member is not None
        assert member.name == "OldName"
        assert member.role == "custom"
        assert member.can_write is False

    def test_does_not_git_commit(self, tmp_path):
        """_save_members must NOT make an implicit 'update team roster' commit."""
        import utils.project_awareness as pa

        project_path = str(tmp_path)
        pa.init_project_config(project_path, "proj")

        projects_mod = MagicMock()
        projects_mod._PROJECTS_DIR_REF = [str(tmp_path)]
        ph = _pa_handler(projects_mod, FakeGLib())
        self._open_active(ph, "proj", project_path)

        ph._git_commit_if_available = MagicMock()

        ph._save_members("proj", ["agent:x"])

        ph._git_commit_if_available.assert_not_called()

    def test_refreshes_awareness_snapshot(self, tmp_path, monkeypatch):
        """_save_members refreshes the awareness snapshot after team save."""
        import utils.project_awareness as pa

        project_path = str(tmp_path)
        pa.init_project_config(project_path, "proj")

        spy = MagicMock()
        monkeypatch.setattr("utils.project_awareness.save_awareness_snapshot", spy)

        projects_mod = MagicMock()
        projects_mod._PROJECTS_DIR_REF = [str(tmp_path)]
        ph = _pa_handler(projects_mod, FakeGLib())
        self._open_active(ph, "proj", project_path)

        ph._save_members("proj", ["agent:x"])

        spy.assert_called_once()

    def test_ordering_equals_input(self, tmp_path, monkeypatch):
        """_save_members preserves input order for existing + new members."""
        from agent.special_agents import SpecialAgentDef
        from models.team import ProjectTeam, TeamMember
        import utils.project_awareness as pa

        project_path = str(tmp_path)
        pa.init_project_config(project_path, "proj")
        pa.save_team(project_path, ProjectTeam(members=[
            TeamMember(session_key="agent:coder", name="Coder", role="coder", can_write=True),
        ]))

        def _fake_get(prefix):
            if prefix == "agent:supervisor":
                return SpecialAgentDef(
                    conv_id_prefix="agent:supervisor",
                    display_name="Supervisor",
                    role="supervisor",
                    emoji="🧭",
                    tools=["read_file", "write_file"],
                    can_write=True,
                )
            return None
        monkeypatch.setattr("agent.special_agents.get_special_agent", _fake_get)

        projects_mod = MagicMock()
        projects_mod._PROJECTS_DIR_REF = [str(tmp_path)]
        ph = _pa_handler(projects_mod, FakeGLib())
        self._open_active(ph, "proj", project_path)

        members = ["agent:coder", "agent:qtr:telegram:1", "agent:supervisor"]
        ph._save_members("proj", members)

        team = pa.load_team(project_path)
        assert team.get_session_keys() == members


class TestOnProjectCreatedCallback:
    def test_fires_for_create_not_open(self, tmp_path, fake_glib):
        """set_on_project_created fires for create_project but not open_project."""
        from unittest.mock import MagicMock
        import utils.project_awareness as pa

        projects_mod = MagicMock()
        projects_mod._PROJECTS_DIR_REF = [str(tmp_path)]
        projects_mod.load_projects.return_value = []
        ph = _pa_handler(projects_mod, fake_glib)

        cb = MagicMock()
        ph.set_on_project_created(cb)

        result = ph.create_project("myproj")
        fake_glib.dispatch_all()
        assert result is not None
        cb.assert_called_once_with("myproj", result)

        # Open an existing project — callback must NOT fire again
        ph.open_project("myproj", result)
        fake_glib.dispatch_all()
        assert cb.call_count == 1

    def test_callback_deferred_via_glib(self, tmp_path, fake_glib):
        """The created callback is queued via GLib, not run synchronously."""
        from unittest.mock import MagicMock
        import utils.project_awareness as pa

        projects_mod = MagicMock()
        projects_mod._PROJECTS_DIR_REF = [str(tmp_path)]
        projects_mod.load_projects.return_value = []
        ph = _pa_handler(projects_mod, fake_glib)

        cb = MagicMock()
        ph.set_on_project_created(cb)

        result = ph.create_project("myproj")
        # Not yet dispatched — queued on fake_glib
        cb.assert_not_called()

        fake_glib.dispatch_all()
        cb.assert_called_once_with("myproj", result)
