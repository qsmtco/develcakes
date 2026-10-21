"""Tests for the `feed_card` tool (SPEC-45).

Two layers:
  * the public tool path — `execute_tool("feed_card", ...)` → `agent.group_tools.feed_card`
    → the installed callback, covering the §6.1 forwarding contract and the
    §6.2 rejection matrix;
  * `MainWindow._post_feed_card` itself, driven unbound against a stub window so
    no real GTK window is constructed. The two metadata stamps (session_key,
    tab_key) are the contracts §2.9 pins.

The `execute_tool` tests use the installed callback to capture `fields`, so a
missed positional forward in the tool registration fails here instead of
silently dropping a parameter into `**kwargs`.
"""

import pytest

from agent import group_tools
from agent.tools import (
    ToolResult,
    execute_tool,
    get_all_tools,
    get_tool_definitions_for_api,
)
from ui.window import MainWindow

_PROJ = "."


class _ProjectHandler:
    def __init__(self, project_name):
        self._project_name = project_name

    def get_active_project_name(self):
        return self._project_name


class _AgentRuntimeHandler:
    def _reply_key(self, session_key):
        return "project:A"


class _FeedHandler:
    def __init__(self):
        self.cards = []
        self.calls = 0

    def add_card(self, card):
        self.calls += 1
        self.cards.append(card)
        return "card-id-1"


class _Stub:
    def __init__(self, project_name="B"):
        self._project_handler = _ProjectHandler(project_name)
        self._agent_runtime_handler = _AgentRuntimeHandler()
        self._feed_handler = _FeedHandler()


_FIELDS = {
    "card_type": "diff",
    "title": "t",
    "body": "b",
    "file_path": "x.py",
    "additions": 1,
    "deletions": 2,
    "task_id": "WU-9",
}


def test_divergence():
    """DESIGN PIN — moved from test_agent_runtime.py::TestCrabcardTabStamping
    ::test_multiproject_divergence_is_deliberate (SPEC-45 §2.9).

    project_name and tab_key come from DIFFERENT sources BY DESIGN. project_name
    stamps the ACTIVE project (the card feeds where the PM is looking); tab_key
    resolves the emitting agent's own routed tab (where the turn rendered). With
    the agent routed to project A while project B is active, the divergence
    project_name="B" / tab_key="project:A" is deliberate — do NOT unify the
    sources.
    """
    stub = _Stub()
    result = MainWindow._post_feed_card(stub, "special:coder", _FIELDS)
    card = stub._feed_handler.cards[0]
    assert card.project_name == "B"
    assert card.metadata["tab_key"] == "project:A"
    assert card.metadata["session_key"] == "special:coder"
    assert result.success is True


def test_project_handler_none_falls_back_to_runtime_active_project():
    """BUG #3: when the project handler reports no project but the agent
    runtime has an active project, the card posts to the RUNTIME project
    (the same source the turn guard reads), instead of failing with
    "feed_card needs an open project". The project_name/tab_key divergence
    pin is preserved — tab_key still comes from _reply_key."""
    stub = _Stub(project_name=None)
    stub._agent_runtime_handler._active_project = ("runtimep", "/path/to/runtimep")
    result = MainWindow._post_feed_card(stub, "special:coder", _FIELDS)
    assert result.success is True, result
    card = stub._feed_handler.cards[0]
    assert card.project_name == "runtimep"
    assert card.metadata["tab_key"] == "project:A"
    assert card.metadata["session_key"] == "special:coder"


def test_both_projects_none_still_fails():
    """When both the project handler and the runtime report no project, the
    card is rejected exactly as before (no feed cards exist at all)."""
    stub = _Stub(project_name=None)
    result = MainWindow._post_feed_card(stub, "special:coder", _FIELDS)
    assert result.success is False
    assert "feed_card needs an open project" in result.error
    assert stub._feed_handler.cards == []


def test_happy_path_maps_fields():
    stub = _Stub()
    result = MainWindow._post_feed_card(stub, "special:coder", _FIELDS)
    card = stub._feed_handler.cards[0]
    assert card.card_type == "diff"
    assert card.title == "t"
    assert card.body == "b"
    assert card.file_path == "x.py"
    assert card.additions == 1
    assert card.deletions == 2
    assert card.task_id == "WU-9"
    assert card.source == "agent"
    assert result.success is True
    assert result.output == "posted diff card card-id-1"


def test_empty_optional_fields_become_none():
    stub = _Stub()
    fields = dict(_FIELDS, file_path="", task_id="")
    result = MainWindow._post_feed_card(stub, "special:coder", fields)
    card = stub._feed_handler.cards[0]
    assert card.file_path is None
    assert card.task_id is None
    assert result.success is True


def test_no_open_project_rejects_without_calling_add_card():
    stub = _Stub(project_name=None)
    result = MainWindow._post_feed_card(stub, "special:coder", _FIELDS)
    assert result.success is False
    assert result.error == "feed_card needs an open project"
    assert stub._feed_handler.calls == 0


def test_author_uses_display_name_when_agent_is_registered(monkeypatch):
    """Registered agent → author is the display name, not the raw suffix."""
    import agent.special_agents as sa

    class _Def:
        display_name = "Coder"

    monkeypatch.setattr(sa, "get_special_agent", lambda sk: _Def())
    stub = _Stub()
    result = MainWindow._post_feed_card(stub, "special:coder", _FIELDS)
    card = stub._feed_handler.cards[0]
    assert card.author == "Coder"
    assert result.success is True


def test_author_fallback_uses_session_suffix(monkeypatch):
    """Unregistered agent → author falls back to the session-key suffix."""
    import agent.special_agents as sa

    monkeypatch.setattr(sa, "get_special_agent", lambda sk: None)
    stub = _Stub()
    result = MainWindow._post_feed_card(stub, "special:coder", _FIELDS)
    card = stub._feed_handler.cards[0]
    assert card.author == "coder"
    assert result.success is True


# ── Public tool path (SPEC-45 §6.1) ─────────────────────────────────────────


@pytest.fixture
def captured(monkeypatch):
    """Install a capturing feed_card callback for the public-path tests.

    Yields the dict the handler passed. The callback returns success so the
    happy-path tests can assert on the propagated ToolResult.
    """
    seen: dict = {}

    def _cb(session_key, fields):
        seen.update(fields)
        seen["session_key"] = session_key
        return ToolResult(success=True, output="posted diff card card-1")

    monkeypatch.setattr(group_tools, "_feed_card_cb", _cb)
    return seen


def test_feed_card_is_registered():
    names = {d["function"]["name"] for d in get_tool_definitions_for_api()}
    assert "feed_card" in names
    assert any(d.name == "feed_card" for d in get_all_tools())


def test_public_path_forwards_every_parameter(captured):
    """All eight parameters reach the handler.

    The registration lambda forwards positionally; a missed forward lands in
    `**kwargs` and is silently dropped, so this is the guard for that.
    """
    result = execute_tool(
        "feed_card",
        {
            "type": "audit_report",
            "title": "T",
            "body": "B",
            "file": "src/x.py",
            "additions": 7,
            "deletions": 3,
            "task_id": "WU-12",
        },
        _PROJ,
        session_key="special:coder",
    )
    assert result.success is True
    assert captured == {
        "card_type": "audit_report",
        "title": "T",
        "body": "B",
        "file_path": "src/x.py",
        "additions": 7,
        "deletions": 3,
        "task_id": "WU-12",
        "session_key": "special:coder",
    }


def test_public_path_minimum_arguments(captured):
    """`type` + `title` alone reach the handler with the defaults intact.

    Asserts the EXACT key set (all seven fields + session_key) so a dropped
    forward cannot masquerade as a defaulted None (BUG #6 hardening). `additions`
    and `deletions` may be None only because they are genuinely defaulted.
    """
    result = execute_tool("feed_card", {"type": "diff", "title": "T"}, _PROJ)
    assert result.success is True
    assert captured == {
        "card_type": "diff",
        "title": "T",
        "body": "",
        "file_path": "",
        "additions": None,
        "deletions": None,
        "task_id": "",
        "session_key": "_unknown",
    }


def test_registration_shape_matches_contract():
    """Registration-shape pin (BUG #6): the first seven positional params of
    the feed_card handler are exactly the spec's field list. A reorder or a
    dropped param goes RED here."""
    import inspect

    from agent.tools import _TOOLS

    params = list(inspect.signature(_TOOLS["feed_card"][1]).parameters)[:7]
    assert params == [
        "type",
        "title",
        "body",
        "file",
        "additions",
        "deletions",
        "task_id",
    ]


def test_counts_are_passed_as_int(captured):
    execute_tool(
        "feed_card",
        {"type": "diff", "title": "T", "additions": 5, "deletions": 2},
        _PROJ,
    )
    assert captured["additions"] == 5
    assert isinstance(captured["additions"], int)
    assert captured["deletions"] == 2
    assert isinstance(captured["deletions"], int)


def test_success_output_contains_card_id(captured):
    """The handler's ToolResult propagates unchanged, card_id included."""
    result = execute_tool("feed_card", {"type": "diff", "title": "T"}, _PROJ)
    assert result.success is True
    assert result.output == "posted diff card card-1"


# ── Rejections (SPEC-45 §6.2) — all success=False, none raise ───────────────


def _reject(arguments):
    result = execute_tool("feed_card", arguments, _PROJ)
    assert result.success is False, result
    return result.error or ""


def test_reject_unknown_type(captured):
    err = _reject({"type": "system", "title": "T"})
    assert "must be one of diff, agent_action, audit_report" in err


def test_reject_missing_type(captured):
    err = _reject({"title": "T"})
    assert "received invalid arguments" in err


def test_reject_empty_title(captured):
    assert "title is empty" in _reject({"type": "diff", "title": ""})


def test_reject_whitespace_only_title(captured):
    assert "title is empty" in _reject({"type": "diff", "title": "   "})


def test_reject_title_too_long(captured):
    assert "title exceeds 200" in _reject({"type": "diff", "title": "x" * 201})


def test_reject_body_too_long(captured):
    assert "body exceeds 100000" in _reject(
        {"type": "diff", "title": "T", "body": "x" * 100_001}
    )


@pytest.mark.parametrize(
    "path",
    ["/etc/passwd", "../../x.py", "C:/x.py", "~/x.py", "a/C:/x.py"],
)
def test_reject_escaping_file_paths(captured, path):
    assert "project-relative path" in _reject(
        {"type": "diff", "title": "T", "file": path}
    )


def test_reject_non_integer_additions(captured):
    assert "additions must be an integer" in _reject(
        {"type": "diff", "title": "T", "additions": "5"}
    )


def test_reject_bool_additions(captured):
    """bool is an int subclass — it must still be rejected."""
    assert "additions must be an integer" in _reject(
        {"type": "diff", "title": "T", "additions": True}
    )


def test_reject_negative_additions(captured):
    assert "must not be negative" in _reject(
        {"type": "diff", "title": "T", "additions": -1}
    )


def test_reject_when_callback_not_installed(monkeypatch):
    monkeypatch.setattr(group_tools, "_feed_card_cb", None)
    result = execute_tool("feed_card", {"type": "diff", "title": "T"}, _PROJ)
    assert result.success is False
    assert "feed_card is not wired" in (result.error or "")


def test_reject_when_callback_raises(monkeypatch):
    """A raising callback is converted, never propagated."""

    def _boom(session_key, fields):
        raise RuntimeError("nope")

    monkeypatch.setattr(group_tools, "_feed_card_cb", _boom)
    result = execute_tool("feed_card", {"type": "diff", "title": "T"}, _PROJ)
    assert result.success is False
    assert "RuntimeError" in (result.error or "")
