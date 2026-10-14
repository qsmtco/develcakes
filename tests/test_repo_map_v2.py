"""SPEC-31: dirty-first repo map, edit-anchored signatures, freshness, preambles."""
from __future__ import annotations

import subprocess

from agent.repo_map import _HUNK_LINE_CAP, _diff_anchors, _git_metadata, _hunk_range, build_repo_map
from agent.runtime import REPO_MAP_HEADER, WRITE_REPLAY_PREAMBLE, AgentRuntime
from models.conversation import Conversation
from tests.test_agent_runtime import _make_cfg
from utils.project_awareness import build_awareness_dict, init_project_config, record_fact
from utils.git_ops import get_head_sha


def _git_init(path):
    subprocess.check_call(["git", "init"], cwd=path, stdout=subprocess.DEVNULL)
    subprocess.check_call(["git", "config", "user.email", "t@example.com"], cwd=path)
    subprocess.check_call(["git", "config", "user.name", "Test"], cwd=path)


def _commit(path, message):
    subprocess.check_call(["git", "add", "-A"], cwd=path)
    subprocess.check_call(
        ["git", "commit", "-m", message],
        cwd=path,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def test_dirty_file_ranks_first(tmp_path):
    _git_init(tmp_path)
    (tmp_path / "clean.py").write_text("def clean_target():\n    return 1\n")
    _commit(tmp_path, "init")
    (tmp_path / "dirty.py").write_text("def dirty_target():\n    return 2\n")
    text = build_repo_map(str(tmp_path), "clean_target")
    assert text.startswith("dirty.py\n")
    assert text.index("dirty.py") < text.index("clean.py")
    assert "def dirty_target" in text


def test_edit_anchored_signature_selection(tmp_path):
    _git_init(tmp_path)
    lines = [""] * 101
    lines[9] = "def alpha():"
    lines[10] = "    return 1"
    lines[99] = "def beta():"
    lines[100] = "    return 1"
    (tmp_path / "work.py").write_text("\n".join(lines) + "\n")
    _commit(tmp_path, "init")
    lines[100] = "    return 2"
    (tmp_path / "work.py").write_text("\n".join(lines) + "\n")
    text = build_repo_map(str(tmp_path), "alpha")
    assert "def beta" in text
    assert "def alpha" not in text
    assert text.startswith("work.py\n")


def test_untracked_falls_back_to_symbol_match(tmp_path):
    _git_init(tmp_path)
    (tmp_path / "other.py").write_text("def other():\n    return 0\n")
    _commit(tmp_path, "init")
    (tmp_path / "track_me.py").write_text(
        "def helper():\n    return 0\n\ndef target_symbol():\n    return 1\n"
    )
    text = build_repo_map(str(tmp_path), "please use target_symbol")
    assert "track_me.py" in text
    assert "def target_symbol" in text
    assert "def helper" not in text


def test_clean_file_stub_demoted(tmp_path):
    _git_init(tmp_path)
    funcs = "\n\n".join(f"def prod_{i:02d}():\n    return {i}" for i in range(30))
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mod.py").write_text(funcs + "\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_fake.py").write_text("def open(self):\n    return None\n")
    _commit(tmp_path, "init")
    text = build_repo_map(str(tmp_path), "please open the resource")
    assert "def prod_00" in text
    if "def open" in text:
        assert text.index("def prod_29") < text.index("def open")
    else:
        assert "def open" not in text


def test_product_start_is_not_demoted(tmp_path):
    _git_init(tmp_path)
    (tmp_path / "zzz.py").write_text("def start():\n    return 1\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "aaa_test.py").write_text("def start():\n    return 0\n")
    _commit(tmp_path, "init")
    text = build_repo_map(str(tmp_path), "start")
    assert "zzz.py" in text
    if "tests/aaa_test.py" in text:
        assert text.index("zzz.py") < text.index("tests/aaa_test.py")


def test_recent_file_outranks_stale(tmp_path, monkeypatch):
    _git_init(tmp_path)
    (tmp_path / "aaa_stale.py").write_text("def stale_marker():\n    return 1\n")
    _commit(tmp_path, "old")
    (tmp_path / "zzz_recent.py").write_text("def recent_marker():\n    return 2\n")
    _commit(tmp_path, "new")
    monkeypatch.setattr("agent.repo_map._RECENCY_COMMITS", 1)
    text = build_repo_map(str(tmp_path), "unrelated_goal")
    assert text.index("zzz_recent.py") < text.index("aaa_stale.py")


def test_edit_deep_in_last_def_follows_that_def(tmp_path):
    _git_init(tmp_path)
    lines = [""] * 400
    lines[9] = "def alpha():"
    lines[10] = "    return 1"
    lines[19] = "def beta():"
    lines[20] = "    return 1"
    lines[399] = "sentinel = 1"
    (tmp_path / "far.py").write_text("\n".join(lines) + "\n")
    _commit(tmp_path, "init")
    lines[399] = "sentinel = 2"
    (tmp_path / "far.py").write_text("\n".join(lines) + "\n")
    text = build_repo_map(str(tmp_path), "alpha")
    assert "def beta" in text
    assert "def alpha" not in text


def test_multiline_hunk_follows_the_def_that_holds_most_lines(tmp_path):
    _git_init(tmp_path)
    lines = [""] * 60
    lines[9] = "def alpha():"
    lines[10] = "    return 1"
    for index in range(11, 39):
        lines[index] = "    x = 1"
    lines[39] = "def beta():"
    lines[40] = "    return 1"
    for index in range(41, 60):
        lines[index] = "    y = 1"
    (tmp_path / "span.py").write_text("\n".join(lines) + "\n")
    _commit(tmp_path, "init")
    for index in range(37, 39):
        lines[index] = "    z = 2"
    lines[39] = "def beta():  # touched"
    for index in range(40, 60):
        lines[index] = "    z = 2"
    (tmp_path / "span.py").write_text("\n".join(lines) + "\n")
    text = build_repo_map(str(tmp_path), "alpha")
    assert "def beta():  # touched" in text
    assert "def alpha" not in text


def test_deleted_dirty_file_is_shown(tmp_path):
    _git_init(tmp_path)
    (tmp_path / "keeper.py").write_text("def keeper_symbol():\n    return 1\n")
    (tmp_path / "gone.py").write_text("def gone_symbol():\n    return 2\n")
    _commit(tmp_path, "init")
    subprocess.check_call(["git", "rm", "-q", "gone.py"], cwd=tmp_path)
    text = build_repo_map(str(tmp_path), "keeper_symbol")
    assert text.startswith("gone.py\n")
    assert "(deleted)" in text
    assert "def gone_symbol" not in text
    assert text.index("gone.py") < text.index("keeper.py")


def test_deleted_file_sorts_among_dirty_files(tmp_path):
    _git_init(tmp_path)
    (tmp_path / "zzz.py").write_text("def alpha():\n    return 1\n")
    (tmp_path / "aaa.py").write_text("def other():\n    return 1\n")
    _commit(tmp_path, "init")
    (tmp_path / "zzz.py").write_text("def alpha():\n    return 9\n")
    subprocess.check_call(["git", "rm", "-q", "aaa.py"], cwd=tmp_path)
    text = build_repo_map(str(tmp_path), "unrelated_goal")
    assert text.index("aaa.py") < text.index("zzz.py")
    assert text.split("zzz.py", 1)[0].count("(deleted)") == 1


def test_edited_file_sorts_before_a_later_deleted_path(tmp_path):
    _git_init(tmp_path)
    (tmp_path / "aaa.py").write_text("def alpha():\n    return 1\n")
    (tmp_path / "zzz.py").write_text("def other():\n    return 1\n")
    _commit(tmp_path, "init")
    (tmp_path / "aaa.py").write_text("def alpha():\n    return 9\n")
    subprocess.check_call(["git", "rm", "-q", "zzz.py"], cwd=tmp_path)
    text = build_repo_map(str(tmp_path), "unrelated_goal")
    assert text.index("aaa.py") < text.index("zzz.py")
    assert "def alpha" in text
    assert "(deleted)" in text.split("aaa.py", 1)[1]


def test_deleted_file_alone_still_renders(tmp_path):
    _git_init(tmp_path)
    (tmp_path / "gone.py").write_text("def gone_symbol():\n    return 1\n")
    _commit(tmp_path, "init")
    subprocess.check_call(["git", "rm", "-q", "gone.py"], cwd=tmp_path)
    text = build_repo_map(str(tmp_path), "gone_symbol")
    assert text == "gone.py\n  (deleted)\n"


def test_comment_only_dirty_file_is_not_marked_deleted(tmp_path):
    _git_init(tmp_path)
    (tmp_path / "note.py").write_text("def keep():\n    return 1\n")
    _commit(tmp_path, "init")
    (tmp_path / "note.py").write_text("# just a comment\n")
    text = build_repo_map(str(tmp_path), "keep")
    assert "(deleted)" not in text
    assert (tmp_path / "note.py").is_file()


def test_hunk_range_caps_and_ignores_dev_null(tmp_path, monkeypatch):
    assert _hunk_range("@@ -1 +2 @@") == (2, 2)
    assert _hunk_range("@@ -1,3 +0,0 @@") == (1, 1)
    assert _hunk_range("@@ -4,2 +5,0 @@") == (5, 5)
    assert _hunk_range("not a hunk") is None
    huge = _hunk_range("@@ -1 +1,999999999 @@")
    assert huge is not None
    assert huge[0] == 1
    assert huge[1] - huge[0] + 1 == _HUNK_LINE_CAP

    def fake_run(args, **_kwargs):
        class Proc:
            stdout = ""
            returncode = 0
        proc = Proc()
        if "diff" in args:
            proc.stdout = (
                "+++ b/a.py\n"
                "@@ -1 +1,999999999 @@\n"
                "+++ /dev/null\n"
                "@@ -1,3 +0,0 @@\n"
                "+++ b/b.py\n"
                "@@ -4,2 +5,0 @@\n"
            )
        return proc

    monkeypatch.setattr("agent.repo_map.subprocess.run", fake_run)
    anchors = _diff_anchors(str(tmp_path), {"a.py", "b.py"})
    assert anchors["a.py"] == [(1, _HUNK_LINE_CAP)]
    assert anchors["b.py"] == [(5, 5)]


def test_rename_status_uses_new_path(tmp_path):
    _git_init(tmp_path)
    (tmp_path / "keeper.py").write_text("def keeper_symbol():\n    return 1\n")
    (tmp_path / "old_name.py").write_text("def renamed_symbol():\n    return 2\n")
    _commit(tmp_path, "init")
    subprocess.check_call(["git", "mv", "old_name.py", "new_name.py"], cwd=tmp_path)
    text = build_repo_map(str(tmp_path), "keeper_symbol")
    assert text.startswith("new_name.py\n")
    assert "old_name.py" not in text
    assert "def renamed_symbol" in text


def test_tree_version_invalidates_cache(tmp_path):
    _git_init(tmp_path)
    (tmp_path / "a.py").write_text("def alpha():\n    return 1\n")
    _commit(tmp_path, "init")
    rt = AgentRuntime(_make_cfg())
    sk = "special:treever"
    rt.create_conversation("Coder", sk, str(tmp_path), defer_prompt_build=True)
    rt._running = True
    conv = rt.get_conversation(sk)
    conv.add_user_message("look at alpha")
    first = rt._repo_map_block_for(sk, conv)
    assert "a.py" in first
    assert "b.py" not in first
    (tmp_path / "b.py").write_text("def beta():\n    return 2\n")
    second = rt._repo_map_block_for(sk, conv)
    assert second == first
    assert "b.py" not in second
    rt._repo_map_tree_version += 1
    third = rt._repo_map_block_for(sk, conv)
    assert "b.py" in third
    assert third.startswith("b.py\n")

    version = rt._repo_map_tree_version
    rt._execute_tool_batch(
        sk, None, conv, "editing",
        [("c1", "edit_file", {"path": "a.py", "old_text": "    return 1\n", "new_text": "    return 3\n"})],
        1,
    )
    assert rt._repo_map_tree_version == version + 1
    rt._execute_tool_batch(
        sk, None, conv, "missing",
        [("c2", "edit_file", {"path": "a.py", "old_text": "nope", "new_text": "x"})],
        2,
    )
    assert rt._repo_map_tree_version == version + 1
    rt._execute_tool_batch(
        sk, None, conv, "writing",
        [("c3", "write_file", {"path": "notes.md", "content": "# hi\n"})],
        3,
    )
    assert rt._repo_map_tree_version == version + 2


def test_preambles_present(tmp_path):
    assert "This is not a task, not an error" in REPO_MAP_HEADER
    assert "Entries may be stale — confirm with git/grep before acting." in REPO_MAP_HEADER
    assert "signatures are anchored to current edits." in REPO_MAP_HEADER

    _git_init(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n")
    _commit(tmp_path, "init")
    init_project_config(str(tmp_path), "p")
    sha = get_head_sha(str(tmp_path)).sha
    record_fact(str(tmp_path), "a.py: syntax passed", ["a.py"], sha, "special:coder")
    memory = build_awareness_dict(str(tmp_path))["PROJECT_MEMORY"]
    preamble = "Live facts, sha-scoped, self-invalidate when files change — verify before acting."
    open_at = memory.index('<untrusted-project-data source="facts.json">')
    close_at = memory.index("</untrusted-project-data>")
    pre_at = memory.index(preamble)
    assert open_at < pre_at < close_at
    assert pre_at < memory.index("a.py: syntax passed")

    rt = AgentRuntime(_make_cfg())
    conv = Conversation(agent_name="Coder", system_prompt="SYS", project_path=str(tmp_path))
    conv.add_user_message("fix the parser")
    packet = {"goal": "fix the parser", "after": len(conv.messages), "hunk": "HUNK", "checks": []}
    wired = rt._compiled_messages(conv, packet, "")
    user = next(item["content"] for item in wired if item["role"] == "user")
    assert WRITE_REPLAY_PREAMBLE in user
    assert user.index(WRITE_REPLAY_PREAMBLE) < user.index("## Write")


def test_git_absent_never_raises(tmp_path):
    assert _git_metadata("/nonexistent") == (set(), set())
    assert _diff_anchors("/nonexistent", {"a.py"}) == {}
    assert build_repo_map("/nonexistent", "goal") == ""
    (tmp_path / "a.py").write_text("def alpha():\n    return 1\n")
    text = build_repo_map(str(tmp_path), "alpha")
    assert "def alpha" in text


def test_git_timeout_still_builds_the_map(tmp_path, monkeypatch):
    def boom(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="git", timeout=5)

    monkeypatch.setattr("agent.repo_map.subprocess.run", boom)
    assert _git_metadata(str(tmp_path)) == (set(), set())
    assert _diff_anchors(str(tmp_path), {"a.py"}) == {}
    (tmp_path / "a.py").write_text("def alpha():\n    return 1\n")
    text = build_repo_map(str(tmp_path), "alpha")
    assert "def alpha" in text


def test_unicode_dirty_path_ranks_first(tmp_path):
    _git_init(tmp_path)
    (tmp_path / "clean.py").write_text("def clean_target():\n    return 1\n")
    _commit(tmp_path, "init")
    (tmp_path / "café.py").write_text("def unic():\n    return 1\n")
    text = build_repo_map(str(tmp_path), "clean_target")
    assert text.startswith("café.py\n")
    assert "def unic" in text


def test_duplicate_name_anchors_to_the_edited_def(tmp_path):
    _git_init(tmp_path)
    original = "def run(extra):\n    return 1\n\ndef run():\n    return 2\n"
    (tmp_path / "dup.py").write_text(original)
    _commit(tmp_path, "init")
    (tmp_path / "dup.py").write_text(
        "def run(extra):\n    return 9\n\ndef run():\n    return 2\n"
    )
    text = build_repo_map(str(tmp_path), "unrelated_goal")
    assert "def run(extra)" in text
    assert "def run():" not in text


def test_malformed_diff_does_not_raise(tmp_path, monkeypatch):
    def fake_run(args, **_kwargs):
        class Proc:
            stdout = ""
            returncode = 0
        proc = Proc()
        if "diff" in args:
            proc.stdout = "not a diff\n@@ garbage @@\n+++ b/\n@@ -1 +1 @@\n"
        elif "status" in args:
            proc.stdout = "?? a.py\n"
        return proc

    monkeypatch.setattr("agent.repo_map.subprocess.run", fake_run)
    anchors = _diff_anchors(str(tmp_path), {"a.py"})
    assert isinstance(anchors, dict)
