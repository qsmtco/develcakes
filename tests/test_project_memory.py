"""project_memory files stay inside .crabcakes/memory and hide superseded topics."""

import os
from types import SimpleNamespace

from agent.tools import ToolResult, execute_tool
from utils.project_memory import load_index, slugify


def _as_supervisor(monkeypatch):
    supervisor = SimpleNamespace(
        role="supervisor", conv_id_prefix="special:supervisor", display_name="Supervisor",
    )
    monkeypatch.setattr(
        "agent.special_agents.get_special_agent",
        lambda key: supervisor if key == "special:supervisor" else None,
    )


def _promote(tmp_path, monkeypatch, slug, body="body line"):
    _as_supervisor(monkeypatch)
    return execute_tool(
        "project_memory",
        {"action": "promote", "name": slug, "body": body},
        str(tmp_path),
        session_key="special:supervisor",
    )


def test_log_appends(tmp_path):
    result = execute_tool(
        "project_memory",
        {"action": "log", "body": "saw the auth bug"},
        str(tmp_path),
        session_key="special:coder",
    )
    assert result.success is True
    assert result.output.startswith("logged log/")
    log_dir = tmp_path / ".crabcakes" / "memory" / "log"
    files = list(log_dir.glob("*.md"))
    assert len(files) == 1
    assert "saw the auth bug" in files[0].read_text(encoding="utf-8")


def test_promote_writes_index_and_eighty_first_is_refused(tmp_path, monkeypatch):
    for i in range(80):
        result = _promote(tmp_path, monkeypatch, f"topic-{i}", body=f"hook {i}")
        assert result.success is True, result.error
    index = load_index(str(tmp_path))
    assert "topic-0" in index
    assert len([ln for ln in index.splitlines() if ln.strip()]) == 80
    refused = _promote(tmp_path, monkeypatch, "topic-80", body="should not land")
    assert refused.success is False
    assert "index is full" in refused.error
    assert not (tmp_path / ".crabcakes" / "memory" / "topic-80.md").exists()


def test_search_omits_superseded(tmp_path, monkeypatch):
    assert _promote(tmp_path, monkeypatch, "auth", body="use argon2").success
    assert _promote(tmp_path, monkeypatch, "auth-old", body="use bcrypt").success
    gone = execute_tool(
        "project_memory",
        {"action": "supersede", "name": "auth-old", "body": "auth"},
        str(tmp_path),
        session_key="special:supervisor",
    )
    assert gone.success is True
    found = execute_tool(
        "project_memory",
        {"action": "search", "query": "bcrypt"},
        str(tmp_path),
        session_key="special:coder",
    )
    assert found.success is True
    assert found.output == "No memories match."
    live = execute_tool(
        "project_memory",
        {"action": "search", "query": "argon2"},
        str(tmp_path),
        session_key="special:coder",
    )
    assert "auth:" in live.output


def test_dotdot_slug_does_not_escape(tmp_path, monkeypatch):
    assert slugify("../etc/passwd") == "etc-passwd"
    result = _promote(tmp_path, monkeypatch, "../etc/passwd", body="nope")
    assert result.success is True
    root = (tmp_path / ".crabcakes" / "memory").resolve()
    written = (root / "etc-passwd.md").resolve()
    assert written.is_file()
    assert str(written).startswith(str(root) + os.sep)
    assert not (tmp_path / "etc").exists()
    outside = execute_tool(
        "project_memory",
        {"action": "promote", "name": "..", "body": "x"},
        str(tmp_path),
        session_key="special:supervisor",
    )
    assert outside.success is False
    assert outside.error == "memory name is empty"


def test_coder_cannot_promote(tmp_path, monkeypatch):
    coder = SimpleNamespace(role="coder", conv_id_prefix="special:coder", display_name="Coder")
    monkeypatch.setattr(
        "agent.special_agents.get_special_agent",
        lambda key: coder if key == "special:coder" else None,
    )
    result = execute_tool(
        "project_memory",
        {"action": "promote", "name": "auth", "body": "secret"},
        str(tmp_path),
        session_key="special:coder",
    )
    assert result.success is False
    assert result.error == "promote is supervisor-only"
    assert not (tmp_path / ".crabcakes" / "memory" / "auth.md").exists()
    assert isinstance(result, ToolResult)


def _log(tmp_path, name="", body="the auth bug lives in login()", scope="shared"):
    return execute_tool(
        "project_memory",
        {"action": "log", "body": body, "name": name, "scope": scope},
        str(tmp_path),
        session_key="special:coder",
    )


def _search(tmp_path, query, scope="shared"):
    return execute_tool(
        "project_memory",
        {"action": "search", "query": query, "scope": scope},
        str(tmp_path),
        session_key="special:coder",
    )


def _as_role(monkeypatch, role):
    agent = SimpleNamespace(
        role=role, conv_id_prefix=f"special:{role}", display_name=role.title(),
    )
    monkeypatch.setattr(
        "agent.special_agents.get_special_agent",
        lambda key: agent if key == f"special:{role}" else None,
    )


def test_log_named_round_trips_through_read(tmp_path):
    logged = _log(tmp_path, name="auth bug")
    assert logged.success is True
    read = execute_tool(
        "project_memory",
        {"action": "read", "name": "auth bug"},
        str(tmp_path),
        session_key="special:coder",
    )
    assert read.success is True, read.error
    assert "the auth bug lives in login()" in read.output


def test_log_named_round_trips_through_search(tmp_path):
    assert _log(tmp_path, name="auth bug").success
    found = execute_tool(
        "project_memory",
        {"action": "search", "query": "login()"},
        str(tmp_path),
        session_key="special:coder",
    )
    assert found.success is True
    assert "No memories match." not in found.output
    assert "auth-bug:" in found.output or "auth-bug" in found.output


def test_log_named_writes_index_line(tmp_path):
    assert _log(tmp_path, name="auth bug").success
    index = load_index(str(tmp_path))
    assert "- auth-bug — the auth bug lives in login()" in index


def test_log_unnamed_does_not_touch_index(tmp_path):
    assert _log(tmp_path).success
    assert load_index(str(tmp_path)) == ""
    assert not (tmp_path / ".crabcakes" / "memory" / "INDEX.md").exists()


def test_log_at_cap_warns_but_still_writes(tmp_path, monkeypatch):
    _as_supervisor(monkeypatch)
    for i in range(80):
        assert _promote(tmp_path, monkeypatch, f"topic-{i}", body=f"hook {i}").success
    logged = _log(tmp_path, name="overflow topic", body="overflow body")
    assert logged.success is True
    assert "index at cap (80)" in logged.output
    assert "other agents will not see it" in logged.output
    assert "overflow topic" not in load_index(str(tmp_path))
    topic = tmp_path / ".crabcakes" / "memory" / "overflow-topic.md"
    assert topic.is_file()
    assert "overflow body" in topic.read_text(encoding="utf-8")


def test_log_twice_does_not_duplicate_index_line(tmp_path):
    assert _log(tmp_path, name="auth bug").success
    assert _log(tmp_path, name="auth bug", body="second note").success
    lines = [
        ln
        for ln in load_index(str(tmp_path)).splitlines()
        if ln.startswith("- auth-bug ")
    ]
    assert len(lines) == 1
    log_dir = tmp_path / ".crabcakes" / "memory" / "log"
    text = "".join(p.read_text(encoding="utf-8") for p in log_dir.glob("*.md"))
    assert "the auth bug lives in login()" in text
    assert "second note" in text
