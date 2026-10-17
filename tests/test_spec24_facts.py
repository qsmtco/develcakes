"""SPEC-24 Stage B: sha-scoped facts and an append-only context.md."""
from __future__ import annotations

import os
import subprocess

from agent.enforcement import EnforcementCheck
from agent.runtime import AgentRuntime
from agent.tools import _edit_file, _write_file, execute_tool, get_tool_definitions_for_api
from tests.test_agent_runtime import _make_cfg
from utils.git_ops import GitResult, get_head_sha
from utils.project_awareness import (
    build_awareness_dict,
    init_project_config,
    load_facts,
    load_live_facts,
    record_enforcement_fact,
    record_fact,
    save_project_context,
)


def _repo(tmp_path):
    subprocess.check_call(["git", "init"], cwd=tmp_path, stdout=subprocess.DEVNULL)
    subprocess.check_call(["git", "config", "user.email", "t@example.com"], cwd=tmp_path)
    subprocess.check_call(["git", "config", "user.name", "Test"], cwd=tmp_path)
    target = tmp_path / "a.py"
    target.write_text("x = 1\n")
    subprocess.check_call(["git", "add", "a.py"], cwd=tmp_path)
    subprocess.check_call(
        ["git", "commit", "-m", "init"],
        cwd=tmp_path,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return target


def test_record_then_live_drops_when_the_file_changes(tmp_path):
    _repo(tmp_path)
    sha = get_head_sha(str(tmp_path)).sha
    record_fact(str(tmp_path), "a.py: syntax passed", ["a.py"], sha, "special:coder")
    assert load_live_facts(str(tmp_path))[0]["text"] == "a.py: syntax passed"
    (tmp_path / "a.py").write_text("x = 2\n")
    assert load_live_facts(str(tmp_path)) == []
    assert load_facts(str(tmp_path))[0]["sha"] == sha


def test_git_failure_keeps_the_fact(tmp_path, monkeypatch):
    _repo(tmp_path)
    sha = get_head_sha(str(tmp_path)).sha
    record_fact(str(tmp_path), "keep me", ["a.py"], sha, "special:coder")
    (tmp_path / "a.py").write_text("x = 9\n")

    def _down(*_a, **_k):
        return GitResult(success=False, stdout="", error="down", sha=None)

    monkeypatch.setattr("utils.git_ops.diff_file_against_working_tree", _down)
    live = load_live_facts(str(tmp_path))
    assert [row["text"] for row in live] == ["keep me"]


def test_missing_repo_does_not_write(tmp_path):
    check = EnforcementCheck(
        tier="syntax", tool="write_file", file="a.py",
        passed=True, detail="ok", output="", duration_ms=1,
    )
    record_enforcement_fact(str(tmp_path), "special:coder", "a.py", [check])
    assert load_facts(str(tmp_path)) == []
    assert not (tmp_path / ".crabcakes" / "facts.json").exists()


def test_corrupt_facts_are_replaced_on_the_next_record(tmp_path):
    _repo(tmp_path)
    crab = tmp_path / ".crabcakes"
    crab.mkdir()
    (crab / "facts.json").write_text("{not json")
    assert load_facts(str(tmp_path)) == []
    sha = get_head_sha(str(tmp_path)).sha
    record_fact(str(tmp_path), "recovered", ["a.py"], sha, "special:coder")
    rows = load_facts(str(tmp_path))
    assert [row["text"] for row in rows] == ["recovered"]


def test_project_memory_is_live_facts_not_the_diary(tmp_path):
    _repo(tmp_path)
    init_project_config(str(tmp_path), "p")
    save_project_context(str(tmp_path), "## 2026-10-11 — Diary heading\nDIARY_ONLY_MARKER\n")
    sha = get_head_sha(str(tmp_path)).sha
    record_fact(str(tmp_path), "a.py: syntax passed", ["a.py"], sha, "special:coder")
    data = build_awareness_dict(str(tmp_path))
    assert "a.py: syntax passed" in data["PROJECT_MEMORY"]
    assert 'source="facts.json"' in data["PROJECT_MEMORY"]
    assert "DIARY_ONLY_MARKER" not in data["PROJECT_MEMORY"]
    assert "Diary heading" in data["CURRENT_TASK"]


def test_enforced_edit_appends_a_fact(tmp_path):
    _repo(tmp_path)
    rt = AgentRuntime(_make_cfg())
    sk = "special:fact"
    rt.create_conversation("Coder", sk, str(tmp_path), defer_prompt_build=True)
    rt._running = True
    conv = rt.get_conversation(sk)
    conv.add_user_message("change the constant")
    rt._execute_tool_batch(
        sk, None, conv, "editing",
        [("c1", "edit_file", {"path": "a.py", "old_text": "x = 1\n", "new_text": "x = 2\n"})],
        1,
    )
    rows = load_facts(str(tmp_path))
    assert len(rows) == 1
    assert rows[0]["files"] == ["a.py"]
    assert rows[0]["sha"] == get_head_sha(str(tmp_path)).sha
    assert rows[0]["session_key"] == sk
    assert "syntax" in rows[0]["text"]
    # The edit itself differs from HEAD, so the live board drops it.
    assert load_live_facts(str(tmp_path)) == []
    wired = rt._messages_for_model(sk, conv, 128_000)
    user = next(m["content"] for m in wired if m["role"] == "user")
    assert "## Write" in user
    assert "x = 1" in user


def test_context_md_is_append_only(tmp_path):
    crab = tmp_path / ".crabcakes"
    crab.mkdir()
    diary = crab / "context.md"
    diary.write_text("## 2026-10-10 — Old\nPrevious\n")
    approve = lambda *_a: True
    ok = execute_tool(
        "write_file",
        {"path": ".crabcakes/context.md", "content": "## 2026-10-11 — Note\nBody\n"},
        str(tmp_path),
        approval_callback=approve,
    )
    assert ok.success is True
    text = diary.read_text()
    assert "## 2026-10-10 — Old" in text
    assert "## 2026-10-11 — Note" in text
    frozen = diary.read_bytes()
    denied = execute_tool(
        "write_file",
        {"path": ".crabcakes/context.md", "content": "no heading"},
        str(tmp_path),
        approval_callback=approve,
    )
    assert denied.success is False
    assert diary.read_bytes() == frozen
    edited = _edit_file(
        ".crabcakes/context.md", "Previous", "CHANGED", str(tmp_path),
    )
    assert edited.success is False
    assert diary.read_bytes() == frozen
    # The direct writer agrees with execute_tool.
    direct = _write_file(
        ".crabcakes/context.md", "no heading", str(tmp_path),
        approval_callback=approve,
    )
    assert direct.success is False
    assert diary.read_bytes() == frozen


def test_no_new_tool_name():
    names = {item["function"]["name"] for item in get_tool_definitions_for_api()}
    assert names == {
        "read_file", "write_file", "edit_file", "exec_command", "list_files",
        "search_files", "web_search", "web_fetch", "provider_events",
    }


def test_empty_fact_text_does_not_write(tmp_path):
    _repo(tmp_path)
    sha = get_head_sha(str(tmp_path)).sha
    record_fact(str(tmp_path), "  \n  ", ["a.py"], sha, "special:coder")
    record_fact(str(tmp_path), "real", [], sha, "special:coder")
    record_fact(str(tmp_path), "real", ["a.py"], "", "special:coder")
    assert load_facts(str(tmp_path)) == []
    assert not (tmp_path / ".crabcakes" / "facts.json").exists()
