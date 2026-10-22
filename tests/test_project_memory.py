"""project_memory files stay inside .crabcakes/memory and hide superseded topics."""

import os
from types import SimpleNamespace

from agent.tools import ToolResult, execute_tool
from utils.project_memory import load_index, slugify


def _as_supervisor(monkeypatch):
    supervisor = SimpleNamespace(
        role="supervisor", conv_id_prefix="agent:supervisor", display_name="Supervisor",
    )
    monkeypatch.setattr(
        "agent.special_agents.get_special_agent",
        lambda key: supervisor if key == "agent:supervisor" else None,
    )


def _promote(tmp_path, monkeypatch, slug, body="body line"):
    _as_supervisor(monkeypatch)
    return execute_tool(
        "project_memory",
        {"action": "promote", "name": slug, "body": body},
        str(tmp_path),
        session_key="agent:supervisor",
    )


def test_log_appends(tmp_path):
    result = execute_tool(
        "project_memory",
        {"action": "log", "body": "saw the auth bug"},
        str(tmp_path),
        session_key="agent:coder",
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
        session_key="agent:supervisor",
    )
    assert gone.success is True
    found = execute_tool(
        "project_memory",
        {"action": "search", "query": "bcrypt"},
        str(tmp_path),
        session_key="agent:coder",
    )
    assert found.success is True
    assert found.output == "No memories match."
    live = execute_tool(
        "project_memory",
        {"action": "search", "query": "argon2"},
        str(tmp_path),
        session_key="agent:coder",
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
        session_key="agent:supervisor",
    )
    assert outside.success is False
    assert outside.error == "memory name is empty"


def test_coder_cannot_promote(tmp_path, monkeypatch):
    coder = SimpleNamespace(role="coder", conv_id_prefix="agent:coder", display_name="Coder")
    monkeypatch.setattr(
        "agent.special_agents.get_special_agent",
        lambda key: coder if key == "agent:coder" else None,
    )
    result = execute_tool(
        "project_memory",
        {"action": "promote", "name": "auth", "body": "secret"},
        str(tmp_path),
        session_key="agent:coder",
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
        session_key="agent:coder",
    )


def _search(tmp_path, query, scope="shared"):
    return execute_tool(
        "project_memory",
        {"action": "search", "query": query, "scope": scope},
        str(tmp_path),
        session_key="agent:coder",
    )


def _as_role(monkeypatch, role):
    agent = SimpleNamespace(
        role=role, conv_id_prefix=f"agent:{role}", display_name=role.title(),
    )
    monkeypatch.setattr(
        "agent.special_agents.get_special_agent",
        lambda key: agent if key == f"agent:{role}" else None,
    )


def test_log_named_round_trips_through_read(tmp_path):
    logged = _log(tmp_path, name="auth bug")
    assert logged.success is True
    read = execute_tool(
        "project_memory",
        {"action": "read", "name": "auth bug"},
        str(tmp_path),
        session_key="agent:coder",
    )
    assert read.success is True, read.error
    assert "the auth bug lives in login()" in read.output


def test_log_named_round_trips_through_search(tmp_path):
    assert _log(tmp_path, name="auth bug").success
    found = execute_tool(
        "project_memory",
        {"action": "search", "query": "login()"},
        str(tmp_path),
        session_key="agent:coder",
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


def _private_topic(tmp_path, role, slug):
    """Private topics live at memory/agents/<role>/<slug>.md."""
    return tmp_path / ".crabcakes" / "memory" / "agents" / role / f"{slug}.md"


def _meta_of(path):
    """Frontmatter of a topic file, parsed without private helpers."""
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), text
    end = text.find("\n---\n", 4)
    meta = {}
    for line in text[4:end].splitlines():
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip()
    return meta


def _private_search(tmp_path, query):
    """Private search as the supervisor (the role that owns these topics)."""
    return execute_tool(
        "project_memory",
        {"action": "search", "query": query, "scope": "private"},
        str(tmp_path),
        session_key="agent:supervisor",
    )


def _private_log(name, body="the auth bug lives in login()"):
    # dispatch takes role_slug from the CALLER, so a private topic written by
    # the supervisor lives under agents/supervisor/.
    return {"action": "log", "body": body, "name": name, "scope": "private"}


def test_supersede_private_marks_private_file_not_shared(tmp_path, monkeypatch):
    _as_supervisor(monkeypatch)
    assert execute_tool(
        "project_memory", _private_log("auth bug"), str(tmp_path),
        session_key="agent:supervisor",
    ).success
    assert execute_tool(
        "project_memory", _private_log("auth-new", "use argon2"), str(tmp_path),
        session_key="agent:supervisor",
    ).success
    old = _private_topic(tmp_path, "supervisor", "auth-bug")
    new_ = _private_topic(tmp_path, "supervisor", "auth-new")
    assert old.is_file() and new_.is_file()

    result = execute_tool(
        "project_memory",
        {"action": "supersede", "name": "auth bug", "body": "auth-new", "scope": "private"},
        str(tmp_path),
        session_key="agent:supervisor",
    )
    assert result.success is True, result.error
    assert result.output == "superseded auth-bug with auth-new"
    # the private file at agents/<role>/ is the one retired
    meta = _meta_of(old)
    assert meta["status"] == "superseded"
    assert meta["superseded_by"] == "auth-new"
    assert _meta_of(new_)["status"] == "live"
    # the private topic is no longer a topic hit in private search
    found = _private_search(tmp_path, "login()")
    assert found.success is True, found.error
    assert "auth-bug:" not in found.output


def test_supersede_private_does_not_touch_shared_index(tmp_path, monkeypatch):
    # a shared topic with the same slug must not be collateral damage
    assert _promote(tmp_path, monkeypatch, "auth bug", body="shared copy").success
    root = tmp_path / ".crabcakes" / "memory"
    index_path = root / "INDEX.md"
    shared = root / "auth-bug.md"
    index_before = index_path.read_text(encoding="utf-8")
    shared_before = shared.read_text(encoding="utf-8")
    assert "- auth-bug " in index_before

    _as_supervisor(monkeypatch)
    assert execute_tool(
        "project_memory", _private_log("auth bug", "private copy"), str(tmp_path),
        session_key="agent:supervisor",
    ).success
    assert execute_tool(
        "project_memory", _private_log("auth-new"), str(tmp_path),
        session_key="agent:supervisor",
    ).success
    result = execute_tool(
        "project_memory",
        {"action": "supersede", "name": "auth bug", "body": "auth-new", "scope": "private"},
        str(tmp_path),
        session_key="agent:supervisor",
    )
    assert result.success is True, result.error
    # private original retired...
    assert _meta_of(_private_topic(tmp_path, "supervisor", "auth-bug"))["status"] == "superseded"
    # ...while the shared topic and INDEX.md are byte-identical
    assert index_path.read_text(encoding="utf-8") == index_before
    assert shared.read_text(encoding="utf-8") == shared_before
    assert _meta_of(shared)["status"] == "live"


def test_promote_private_moves_body_and_marks_original(tmp_path, monkeypatch):
    _as_supervisor(monkeypatch)
    private_body = "curated private finding: argon2 params live in auth.py"
    assert execute_tool(
        "project_memory", _private_log("argon2", private_body), str(tmp_path),
        session_key="agent:supervisor",
    ).success
    private = _private_topic(tmp_path, "supervisor", "argon2")
    assert private.is_file()
    assert load_index(str(tmp_path)) == ""  # private topics have no index line

    promoted = execute_tool(
        "project_memory",
        {"action": "promote", "name": "argon2", "body": "ignored stub", "scope": "private"},
        str(tmp_path),
        session_key="agent:supervisor",
    )
    assert promoted.success is True, promoted.error
    shared = tmp_path / ".crabcakes" / "memory" / "argon2.md"
    shared_text = shared.read_text(encoding="utf-8")
    # the curated private body is what became shared, not the promote body
    assert private_body in shared_text
    assert "ignored stub" not in shared_text
    assert _meta_of(shared)["status"] == "live"
    assert "- argon2 — " in load_index(str(tmp_path))
    # exactly one live copy: the private original is retired in place
    meta = _meta_of(private)
    assert meta["status"] == "superseded"
    assert meta["superseded_by"] == "argon2"
    assert "argon2:" not in _private_search(tmp_path, "argon2").output
    assert "argon2:" in _search(tmp_path, "argon2", scope="shared").output


def test_promote_shared_unchanged_behavior(tmp_path, monkeypatch):
    _as_supervisor(monkeypatch)
    result = execute_tool(
        "project_memory",
        {"action": "promote", "name": "gateway", "body": "routed through the bridge"},
        str(tmp_path),
        session_key="agent:supervisor",
    )
    assert result.success is True, result.error
    assert result.output == "promoted gateway"
    shared = tmp_path / ".crabcakes" / "memory" / "gateway.md"
    meta = _meta_of(shared)
    assert meta["status"] == "live"
    assert meta["superseded_by"] == ""
    assert meta["origin"] == "promote"
    assert "routed through the bridge" in shared.read_text(encoding="utf-8")
    assert "- gateway — routed through the bridge" in load_index(str(tmp_path))
    # a shared promote creates no private tree and no extra INDEX lines
    assert not (tmp_path / ".crabcakes" / "memory" / "agents").exists()
    assert len([ln for ln in load_index(str(tmp_path)).splitlines() if ln.strip()]) == 1
