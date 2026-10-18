"""SPEC-23: one tool-message copy, same-turn read dedup, task card."""
from __future__ import annotations

import json
import os
from unittest.mock import patch

from agent.llm.convert import convert_messages_for_anthropic
from agent.runtime import AgentRuntime
from models.conversation import Conversation, MessageRole
from tests.test_agent_runtime import _make_cfg, _resp


def _runtime(project: str):
    rt = AgentRuntime(_make_cfg())
    sk = "special:coder"
    rt.create_conversation("Coder", sk, project, defer_prompt_build=True)
    return rt, sk, rt.get_conversation(sk)


def _tool_bodies(conv):
    return [m.content for m in conv.messages if m.role == MessageRole.TOOL_RESULT]


def test_third_identical_read_is_refused(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("hello world\n")
    rt, sk, conv = _runtime(str(tmp_path))
    for n in (1, 2, 3):
        assert rt._execute_tool_batch(
            sk, None, conv, "reading", [("c%d" % n, "read_file", {"path": "a.txt"})], n,
        ) is False
    bodies = _tool_bodies(conv)
    assert bodies[0] == "hello world\n"
    assert bodies[1].startswith("[unchanged")
    assert bodies[2].startswith("REFUSED:")
    assert "hello world" not in bodies[2]


def test_shell_reprint_of_a_read_file_is_refused(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("hello world\n")
    rt, sk, conv = _runtime(str(tmp_path))
    assert rt._execute_tool_batch(
        sk, None, conv, "reading", [("c1", "read_file", {"path": "a.txt"})], 1,
    ) is False
    assert rt._execute_tool_batch(
        sk, None, conv, "reading",
        [("c2", "exec_command", {"command": "sed -n '1,5p' a.txt"})],
        2,
    ) is False
    bodies = _tool_bodies(conv)
    assert bodies[1].startswith("REFUSED:")
    assert "hello world" not in bodies[1]


def test_second_identical_read_is_unchanged_notice(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("hello world\n")
    rt, sk, conv = _runtime(str(tmp_path))
    first = [("c1", "read_file", {"path": "a.txt"})]
    second = [("c2", "read_file", {"path": "a.txt"})]
    assert rt._execute_tool_batch(sk, None, conv, "reading", first, 1) is False
    assert rt._execute_tool_batch(sk, None, conv, "reading", second, 2) is False
    bodies = _tool_bodies(conv)
    assert bodies[0] == "hello world\n"
    assert bodies[1] == (
        "[unchanged \u2014 read_file a.txt offset=0 limit=None; "
        "the body is already in this conversation. "
        "Do not read it again and do not print it with a shell command.]"
    )
    for msg in conv.messages:
        for tc in msg.tool_calls:
            assert tc.result is None
            assert tc.status.value == "completed"


def test_changed_mtime_is_not_deduped(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("hello world\n")
    rt, sk, conv = _runtime(str(tmp_path))
    rt._execute_tool_batch(sk, None, conv, "reading", [("c1", "read_file", {"path": "a.txt"})], 1)
    st = target.stat()
    os.utime(target, (st.st_atime, st.st_mtime + 10))
    rt._execute_tool_batch(sk, None, conv, "reading", [("c2", "read_file", {"path": "a.txt"})], 2)
    bodies = _tool_bodies(conv)
    assert bodies[1] == "hello world\n"
    assert "unchanged" not in bodies[1]


def test_different_limit_is_not_deduped(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("hello world\n")
    rt, sk, conv = _runtime(str(tmp_path))
    rt._execute_tool_batch(sk, None, conv, "reading", [("c1", "read_file", {"path": "a.txt"})], 1)
    rt._execute_tool_batch(
        sk, None, conv, "reading",
        [("c2", "read_file", {"path": "a.txt", "offset": 0, "limit": 5})],
        2,
    )
    bodies = _tool_bodies(conv)
    assert bodies[1] == "hello"
    assert "unchanged" not in bodies[1]


def test_cleanup_drops_fingerprint_so_next_turn_rereads(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("hello world\n")
    rt, sk, conv = _runtime(str(tmp_path))
    rt._execute_tool_batch(sk, None, conv, "reading", [("c1", "read_file", {"path": "a.txt"})], 1)
    rt._cleanup_tool_history(sk)
    rt._execute_tool_batch(sk, None, conv, "reading", [("c2", "read_file", {"path": "a.txt"})], 2)
    bodies = _tool_bodies(conv)
    assert bodies[1] == "hello world\n"


def test_drop_read_cache_missing_key_is_noop():
    rt = AgentRuntime(_make_cfg())
    rt.drop_read_cache("special:missing")


def test_failed_read_is_not_fingerprinted(tmp_path):
    rt, sk, conv = _runtime(str(tmp_path))
    args = {"path": "missing.txt"}
    rt._execute_tool_batch(sk, None, conv, "reading", [("c1", "read_file", args)], 1)
    rt._execute_tool_batch(sk, None, conv, "reading", [("c2", "read_file", args)], 2)
    bodies = _tool_bodies(conv)
    assert "unchanged" not in bodies[0]
    assert "unchanged" not in bodies[1]
    assert "Not a file" in bodies[0]


def test_denied_exec_clears_tool_call_result(tmp_path):
    rt, sk, conv = _runtime(str(tmp_path))
    rt._execute_tool_batch(
        sk, None, conv, "run",
        [("c1", "exec_command", {"command": "echo hi"})],
        1,
    )
    assistant = next(m for m in conv.messages if m.tool_calls)
    assert assistant.tool_calls[0].result is None
    assert "approval" in _tool_bodies(conv)[0]


def test_empty_project_path_skips_dedup(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("hello world\n")
    rt, sk, conv = _runtime(str(tmp_path))
    # The tool batch refuses an empty project before dedup (workspace
    # validation). The dedup helper itself must still skip.
    conv.project_path = ""
    text = rt._dedup_read_output(sk, conv, {"path": "a.txt"}, "hello world\n")
    assert text == "hello world\n"
    assert sk not in rt._read_fingerprints


def test_stat_oserror_skips_dedup(tmp_path):
    rt, sk, conv = _runtime(str(tmp_path))
    text = rt._dedup_read_output(
        sk, conv, {"path": "no-such-file.txt"}, "body",
    )
    assert text == "body"
    assert rt._read_fingerprints.get(sk, {}) == {}


def test_task_card_round_trip_and_wire_prefix(tmp_path, monkeypatch):
    from agent.persistence import load_conversation_from_disk, save_conversation_to_disk

    conv = Conversation(agent_name="Coder", system_prompt="SYS", model="")
    conv.add_user_message("goal")
    conv.task_card = "read a.py offset=0"
    monkeypatch.setattr("models.conversation._tiktoken_encoding_for", lambda model: None)
    before = conv.get_token_estimate()
    prompt = conv.system_prompt
    conv.task_card = "read a.py offset=0\nedit_file b.py"
    after = conv.get_token_estimate()
    assert after != before
    assert conv.system_prompt == prompt

    no_card = Conversation(agent_name="Coder", system_prompt="SYS")
    no_card.add_user_message("goal")
    wired = conv.to_api_messages()
    assert len(wired) == len(no_card.to_api_messages())
    assert wired[-1]["content"].startswith("read a.py offset=0\nedit_file b.py\n\n")
    assert wired[-1]["content"].endswith("goal")
    anth = convert_messages_for_anthropic(wired)
    plain = convert_messages_for_anthropic(no_card.to_api_messages())
    assert len(anth) == len(plain) == 2
    assert anth[0]["content"] == "SYS"
    assert anth[1]["content"].startswith("read a.py offset=0")

    with patch("agent.persistence.conversations_dir", return_value=str(tmp_path)):
        save_conversation_to_disk(conv, "special:card")
        loaded, _meta = load_conversation_from_disk("special:card")
        assert loaded.task_card == conv.task_card
        raw = json.loads((tmp_path / "special:card.json").read_text())
        del raw["task_card"]
        (tmp_path / "special:card.json").write_text(json.dumps(raw))
        old, _meta = load_conversation_from_disk("special:card")
        assert old.task_card == ""
        raw["task_card"] = 12
        (tmp_path / "special:card.json").write_text(json.dumps(raw))
        bad, _meta = load_conversation_from_disk("special:card")
        assert bad.task_card == ""


def test_task_card_waits_until_a_user_message_exists():
    conv = Conversation(agent_name="Coder", system_prompt="SYS", task_card="pending")
    msgs = conv.to_api_messages()
    assert msgs == [{"role": "system", "content": "SYS"}]


def test_note_task_trims_to_last_500_on_a_newline(tmp_path):
    rt, sk, conv = _runtime(str(tmp_path))
    rt._note_task(sk, "a" * 400)
    rt._note_task(sk, "b" * 400)
    assert len(conv.task_card) <= 500
    assert conv.task_card == "b" * 400
    assert conv.task_card.startswith("b")
    assert "\n" not in conv.task_card[:1]


def test_next_turn_rereads_instead_of_reporting_unchanged(tmp_path):
    (tmp_path / "a.txt").write_text("hello world\n")
    rt = AgentRuntime(_make_cfg())
    sk = "special:turns"
    rt.create_conversation("Coder", sk, str(tmp_path), defer_prompt_build=True)
    rt._running = True
    calls = {"n": 0}

    def llm(session_key, messages, tools, **kwargs):
        calls["n"] += 1
        if calls["n"] in (1, 3):
            return _resp(tool_calls=[{
                "id": f"c{calls['n']}",
                "function": {"name": "read_file", "arguments": json.dumps({"path": "a.txt"})},
            }])
        return _resp("done")

    with patch.object(rt, "_call_llm", llm):
        rt._run_loop(sk, "first")
        rt._run_loop(sk, "second")
    bodies = _tool_bodies(rt.get_conversation(sk))
    assert bodies == ["hello world\n", "hello world\n"]


def test_task_card_prefixes_only_the_last_user_message():
    conv = Conversation(agent_name="Coder", system_prompt="SYS", task_card="CARD")
    conv.add_user_message("first")
    conv.add_assistant_message("ok")
    conv.add_user_message("second")
    wired = conv.to_api_messages()
    users = [m for m in wired if m["role"] == "user"]
    assert users[0]["content"] == "first"
    assert users[1]["content"] == "CARD\n\nsecond"
    assert len(wired) == 4


def test_successful_read_appends_task_line(tmp_path):
    (tmp_path / "a.txt").write_text("hello\n")
    rt, sk, conv = _runtime(str(tmp_path))
    rt._execute_tool_batch(
        sk, None, conv, "reading",
        [("c1", "read_file", {"path": "a.txt", "offset": 2})],
        1,
    )
    # SPEC-24 Stage B owns project facts. The per-session card is no longer
    # appended from the tool batch.
    assert conv.task_card == ""
    rt._execute_tool_batch(
        sk, None, conv, "reading",
        [("c2", "read_file", {"path": "missing.txt"})],
        2,
    )
    assert conv.task_card == ""


def _llm_done(session_key, messages, tools, **kwargs):
    return _resp("done")


def test_system_prompt_rebuilds_only_when_inputs_change(tmp_path):
    """Two turns on an unchanged project build the prompt once.

    Writing .crabcakes/context.md changes the awareness fingerprint and
    the next turn builds exactly once more.
    """
    from agent.context import build_system_prompt as real_build

    crab = tmp_path / ".crabcakes"
    crab.mkdir()
    (crab / "context.md").write_text("# start\n")
    rt = AgentRuntime(_make_cfg())
    sk = "special:coder"
    calls = {"n": 0}

    def counting(*args, **kwargs):
        calls["n"] += 1
        return real_build(*args, **kwargs)

    rt._running = True
    with patch("agent.context.build_system_prompt", side_effect=counting):
        rt.create_conversation(
            "Coder", sk, str(tmp_path), agent_role="coder",
        )
        assert calls["n"] == 1
        with patch.object(rt, "_call_llm", _llm_done):
            rt._run_loop(sk, "first")
            rt._run_loop(sk, "second")
        assert calls["n"] == 1
        (crab / "context.md").write_text("# changed\nunique-awareness-token\n")
        with patch.object(rt, "_call_llm", _llm_done):
            rt._run_loop(sk, "third")
        assert calls["n"] == 2


def test_deferred_prompt_builds_once_on_the_first_turn(tmp_path):
    """defer_prompt_build fills the prompt on the first turn, not twice."""
    from agent.context import build_system_prompt as real_build

    rt = AgentRuntime(_make_cfg())
    sk = "special:coder"
    calls = {"n": 0}

    def counting(*args, **kwargs):
        calls["n"] += 1
        return real_build(*args, **kwargs)

    rt._running = True
    with patch("agent.context.build_system_prompt", side_effect=counting):
        rt.create_conversation(
            "Coder", sk, str(tmp_path), agent_role="coder",
            defer_prompt_build=True,
        )
        assert calls["n"] == 0
        with patch.object(rt, "_call_llm", _llm_done):
            rt._run_loop(sk, "first")
            rt._run_loop(sk, "second")
        assert calls["n"] == 1


def test_fingerprint_catches_subdir_add_and_delete(tmp_path):
    """Bugfix B2 (audit BUG #1): the fingerprint file slot is blind to files
    added/removed inside subdirectories when it stats only the project root —
    a root-only stat never sees ``pkg/newmod.py`` appear or vanish.

    Sequence: two snapshots on an unchanged project must be equal; adding a
    file in a subdirectory must change the fingerprint; deleting it must
    change it again.
    """
    rt, _sk, conv = _runtime(str(tmp_path))
    (tmp_path / "pkg").mkdir()

    before_a = rt._system_prompt_fingerprint(conv)
    before_b = rt._system_prompt_fingerprint(conv)
    assert before_a == before_b

    (tmp_path / "pkg" / "newmod.py").write_text("x = 1\n")
    with_new = rt._system_prompt_fingerprint(conv)
    assert with_new != before_a

    (tmp_path / "pkg" / "newmod.py").unlink()
    after_delete = rt._system_prompt_fingerprint(conv)
    assert after_delete != with_new
    assert after_delete == before_a


def test_fingerprint_catches_rename_within_subdir(tmp_path):
    """Bugfix C4 (audit BUG #2): renaming pkg/a.py → pkg/b.py keeps the
    per-top counts identical, so the count-only tuple missed it while the
    file index (which lists names) did change. A rolling blake2b of the
    walked relative paths folds the rename into the fingerprint. The counts
    slot must be unchanged across the rename — proving the new hash slot is
    what catches it.
    """
    from agent.context import _project_tree_fingerprint

    rt, _sk, conv = _runtime(str(tmp_path))
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.py").write_text("x = 1\n")

    before_a = rt._system_prompt_fingerprint(conv)
    before_b = rt._system_prompt_fingerprint(conv)
    assert before_a == before_b
    counts_a = _project_tree_fingerprint(str(tmp_path))[1]

    (pkg / "a.py").rename(pkg / "b.py")

    assert rt._system_prompt_fingerprint(conv) != before_a
    assert _project_tree_fingerprint(str(tmp_path))[1] == counts_a
