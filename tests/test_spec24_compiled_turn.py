"""SPEC-24 Stage A: compiled wire list after an enforced write."""
from __future__ import annotations

import json
from unittest.mock import patch

from agent.enforcement import EnforcementCheck
from agent.runtime import REPO_MAP_HEADER, WRITE_REPLAY_PREAMBLE, AgentRuntime
from agent.tools import ToolResult
from models.conversation import Conversation, ToolCall
from tests.test_agent_runtime import _make_cfg


def _check(detail="syntax failed", output="boom"):
    return EnforcementCheck(
        tier="syntax",
        tool="edit_file",
        file="a.py",
        passed=False,
        detail=detail,
        output=output,
        duration_ms=1,
    )


def _conv() -> Conversation:
    conv = Conversation(agent_name="Coder", system_prompt="SYS", project_path="/tmp/proj")
    conv.add_user_message("fix the parser")
    tc = ToolCall(call_id="r1", tool_name="read_file", arguments={"path": "a.py"})
    conv.add_assistant_message("reading", [tc])
    conv.add_tool_result("r1", "READBODY-UNIQUE")
    return conv


def test_compile_omits_the_read_and_keeps_the_transcript():
    rt = AgentRuntime(_make_cfg())
    conv = _conv()
    sk = "special:compile"
    rt._conversations[sk] = conv
    before = len(conv.messages)
    result = ToolResult(success=True, output="OK — edited a.py", enforcement_checks=[_check()])
    rt._maybe_compile(
        sk, conv, "edit_file",
        {"path": "a.py", "old_text": "OLD", "new_text": "NEW"},
        result,
    )
    # The write's tool result is what moves `after`. The unit path above
    # did not append it; append it the way the batch does, then set after.
    conv.add_tool_result("w1", "OK — edited a.py")
    rt._compile[sk]["after"] = len(conv.messages)
    wired = rt._messages_for_model(sk, conv, 128_000)
    assert len(conv.messages) == before + 1
    user = next(m for m in wired if m["role"] == "user")
    assert "fix the parser" in user["content"]
    assert "--- old_text" in user["content"]
    assert "OLD" in user["content"]
    assert "syntax failed" in user["content"]
    assert "READBODY-UNIQUE" not in user["content"]
    blob = "\n".join(m.get("content") or "" for m in wired)
    assert "READBODY-UNIQUE" not in blob
    assert conv.messages[2].content == "READBODY-UNIQUE"

    conv.add_assistant_message("applied the fix")
    wired2 = rt._messages_for_model(sk, conv, 128_000)
    blob2 = "\n".join(m.get("content") or "" for m in wired2)
    assert "applied the fix" in blob2
    assert "READBODY-UNIQUE" not in blob2


def test_hunk_is_capped_at_4000_chars():
    rt = AgentRuntime(_make_cfg())
    conv = _conv()
    sk = "special:hunk"
    rt._conversations[sk] = conv
    old = "O" * 5000
    new = "N" * 5000
    rt._maybe_compile(
        sk, conv, "edit_file",
        {"path": "a.py", "old_text": old, "new_text": new},
        ToolResult(success=True, enforcement_checks=[_check()]),
    )
    hunk = rt._compile[sk]["hunk"]
    assert old[:4000] in hunk
    assert "O" * 4001 not in hunk
    assert new[:4000] in hunk
    assert "N" * 4001 not in hunk


def test_write_file_hunk_is_a_byte_count_not_the_content():
    rt = AgentRuntime(_make_cfg())
    conv = _conv()
    sk = "special:write"
    rt._conversations[sk] = conv
    content = "h" * 100
    rt._maybe_compile(
        sk, conv, "write_file",
        {"path": "a.py", "content": content},
        ToolResult(success=True, enforcement_checks=[_check(detail="wrote")]),
    )
    hunk = rt._compile[sk]["hunk"]
    assert hunk == "write_file replaced a.py (100 bytes)"
    assert content not in hunk


def test_plain_budget_counts_repo_map_and_tool_schemas(monkeypatch):
    """§8: the check includes the repo map and the tool-schema JSON.

    The transcript estimate alone is under the ceiling. Either addon by
    itself pushes the same call over, and the error is the existing one.
    """
    monkeypatch.setattr("models.conversation._tiktoken_encoding_for", lambda model: None)
    rt = AgentRuntime(_make_cfg())
    conv = Conversation(
        agent_name="Coder", system_prompt="S" * 40, model="", project_path="/tmp/proj",
    )
    conv.add_user_message("goal")
    sk = "special:addons"
    rt._conversations[sk] = conv
    estimate = conv.get_token_estimate()
    reserve = rt._RESPONSE_RESERVE_TOKENS

    tools = [{"type": "function", "function": {"name": "read_file", "description": "D" * 4000}}]
    schema_tokens = len(json.dumps(tools)) // 4
    model_max = estimate + reserve + max(1, schema_tokens // 2)
    with patch("agent.repo_map.build_repo_map", return_value=""):
        rt._messages_for_model(sk, conv, model_max)
        try:
            rt._messages_for_model(sk, conv, model_max, tools)
            raised = False
        except RuntimeError as exc:
            raised = True
            assert "after compaction" in str(exc)
        assert raised

    rt.drop_repo_map(sk)
    block = "M" * 4000
    map_tokens = len(REPO_MAP_HEADER + "\n\n" + block + "\n\n") // 4
    model_max = estimate + reserve + max(1, map_tokens // 2)
    with patch("agent.repo_map.build_repo_map", return_value=block):
        rt._messages_for_model(sk, conv, model_max + map_tokens)
        try:
            rt._messages_for_model(sk, conv, model_max)
            raised = False
        except RuntimeError as exc:
            raised = True
            assert "after compaction" in str(exc)
        assert raised


def test_compiled_call_skips_transcript_budget_when_packet_fits(monkeypatch):
    monkeypatch.setattr("models.conversation._tiktoken_encoding_for", lambda model: None)
    rt = AgentRuntime(_make_cfg())
    conv = Conversation(agent_name="Coder", system_prompt="SYS", model="")
    conv.add_user_message("goal")
    conv.add_tool_result("r", "Z" * 40_000)
    sk = "special:budget"
    rt._conversations[sk] = conv
    model_max = 8_000
    # Transcript is over the ceiling. Without a packet this raises.
    try:
        rt._messages_for_model(sk, conv, model_max)
        raised = False
    except RuntimeError as exc:
        raised = True
        assert "after compaction" in str(exc)
    assert raised
    rt._maybe_compile(
        sk, conv, "edit_file",
        {"path": "a.py", "old_text": "a", "new_text": "b"},
        ToolResult(success=True, enforcement_checks=[_check()]),
    )
    conv.add_tool_result("w", "OK")
    rt._compile[sk]["after"] = len(conv.messages)
    wired = rt._messages_for_model(sk, conv, model_max)
    assert wired[1]["role"] == "user"
    assert "Z" * 100 not in wired[1]["content"]


def test_compiled_packet_carries_the_repo_map_label():
    """SPEC-24 packet labels its map block with the same constant as the
    normal path, in the same shape. Both attachment points are pinned here
    and in test_repo_map.py, so the two cannot drift apart."""
    rt = AgentRuntime(_make_cfg())
    conv = _conv()
    sk = "special:compiledmap"
    rt._conversations[sk] = conv
    rt._maybe_compile(
        sk, conv, "edit_file",
        {"path": "a.py", "old_text": "OLD", "new_text": "NEW"},
        ToolResult(success=True, enforcement_checks=[_check()]),
    )
    conv.add_tool_result("w1", "OK — edited a.py")
    rt._compile[sk]["after"] = len(conv.messages)
    with patch("agent.repo_map.build_repo_map", return_value="MAPBLOCK") as built:
        wired = rt._messages_for_model(sk, conv, 128_000)
    assert built.call_count == 1
    user = next(m for m in wired if m["role"] == "user")["content"]

    # Goal first, then the labelled block, then the packet sections. The
    # label is the runtime constant, byte for byte — not a copy in this file.
    prefix = (
        "fix the parser\n\n" + REPO_MAP_HEADER + "\n\nMAPBLOCK\n\n"
        + WRITE_REPLAY_PREAMBLE + "\n## Write\n"
    )
    assert user.startswith(prefix)
    assert user.index(REPO_MAP_HEADER) < user.index("MAPBLOCK") < user.index("## Write")

    # Same label on the non-compiled attachment point, for the same block.
    plain = _conv()
    rt._conversations["special:plainmap"] = plain
    with patch("agent.repo_map.build_repo_map", return_value="MAPBLOCK"):
        wired_plain = rt._messages_for_model("special:plainmap", plain, 128_000)
    plain_user = next(m for m in wired_plain if m["role"] == "user")["content"]
    assert plain_user == REPO_MAP_HEADER + "\n\nMAPBLOCK\n\nfix the parser"
    assert user[len("fix the parser\n\n"):].startswith(plain_user[: plain_user.rindex("\n\n") + 2])
    assert REPO_MAP_HEADER in plain_user and REPO_MAP_HEADER in user

    # Negative control: no map block, no label on the packet.
    conv2 = _conv()
    sk2 = "special:compilednomap"
    rt._conversations[sk2] = conv2
    rt._maybe_compile(
        sk2, conv2, "edit_file",
        {"path": "a.py", "old_text": "OLD", "new_text": "NEW"},
        ToolResult(success=True, enforcement_checks=[_check()]),
    )
    conv2.add_tool_result("w1", "OK — edited a.py")
    rt._compile[sk2]["after"] = len(conv2.messages)
    with patch("agent.repo_map.build_repo_map", return_value=""):
        wired2 = rt._messages_for_model(sk2, conv2, 128_000)
    user2 = next(m for m in wired2 if m["role"] == "user")["content"]
    assert REPO_MAP_HEADER not in user2
    assert "MAPBLOCK" not in user2


def test_markdown_write_and_failed_edit_do_not_start_compile(tmp_path):
    rt = AgentRuntime(_make_cfg())
    sk = "special:skip"
    rt.create_conversation("Coder", sk, str(tmp_path), defer_prompt_build=True)
    rt._running = True
    conv = rt.get_conversation(sk)
    conv.add_user_message("note")
    rt._execute_tool_batch(
        sk, None, conv, "writing",
        [("c1", "write_file", {"path": "notes.md", "content": "# hi\n"})],
        1,
    )
    assert sk not in rt._compile
    rt._execute_tool_batch(
        sk, None, conv, "editing",
        [("c2", "edit_file", {"path": "notes.md", "old_text": "nope", "new_text": "x"})],
        2,
    )
    assert sk not in rt._compile


def test_no_user_message_does_not_compile():
    rt = AgentRuntime(_make_cfg())
    conv = Conversation(agent_name="Coder", system_prompt="SYS")
    sk = "special:nouser"
    rt._maybe_compile(
        sk, conv, "edit_file",
        {"path": "a.py", "old_text": "a", "new_text": "b"},
        ToolResult(success=True, enforcement_checks=[_check()]),
    )
    assert sk not in rt._compile


def test_clear_drops_compile_and_the_next_send_uses_the_transcript():
    rt = AgentRuntime(_make_cfg())
    conv = _conv()
    sk = "special:clear"
    rt._conversations[sk] = conv
    rt._compile[sk] = {
        "goal": "fix the parser",
        "path": "a.py",
        "hunk": "--- old_text\nx\n--- new_text\ny",
        "checks": [{"tier": "syntax", "passed": True, "detail": "ok", "output": ""}],
        "after": len(conv.messages),
    }
    rt.drop_compile(sk)
    wired = rt._messages_for_model(sk, conv, 128_000)
    blob = "\n".join(m.get("content") or "" for m in wired)
    assert "READBODY-UNIQUE" in blob
    assert "## Write" not in blob


def test_compiled_budget_counts_the_task_card_once_with_one_estimator(monkeypatch):
    """Debugger BUG #5 + #6 on the compiled branch, pinned together.

    BUG #5: _compiled_messages() emits no task card, so the card has to be
    charged as an addon — exactly once, never on top of a card the wire
    already carries. BUG #6: base and addons must share one estimator.

    The counting paths are instrumented rather than the token numbers, so
    the assertions survive any change to the token constants: the card must
    be measured through one estimator, the same one that measures the wire,
    and the schema JSON must be charged on that same estimator.
    """
    # A fake encoding that counts words, so "one estimator" is observable as
    # "one family of calls" instead of an opaque integer.
    class _WordEncoding:
        name = "word-encoding"

        @staticmethod
        def encode_ordinary(text: str) -> list[int]:
            return text.split()

    monkeypatch.setattr(
        "models.conversation._tiktoken_encoding_for", lambda model: _WordEncoding()
    )

    rt = AgentRuntime(_make_cfg())
    conv = Conversation(
        agent_name="Coder", system_prompt="SYS", model="", project_path="/tmp/proj",
    )
    conv.task_card = "CARD " * 200
    conv.add_user_message("goal")
    sk = "special:cardbudget"
    rt._conversations[sk] = conv
    rt._maybe_compile(
        sk, conv, "edit_file",
        {"path": "a.py", "old_text": "a", "new_text": "b"},
        ToolResult(success=True, enforcement_checks=[_check()]),
    )
    conv.add_tool_result("w", "OK")
    rt._compile[sk]["after"] = len(conv.messages)

    counted: list[tuple[str, int]] = []
    original = conv._count_text

    def _recording(encoding, text: str) -> int:
        counted.append((text, len(text)))
        return original(encoding, text)

    monkeypatch.setattr(conv, "_count_text", _recording)

    tools = [{"type": "function", "function": {"name": "read_file", "description": "D" * 2000}}]
    schema_json = json.dumps(tools)
    with patch("agent.repo_map.build_repo_map", return_value=""):
        rt._messages_for_model(sk, conv, 128_000, tools)

    texts = [text for text, _ in counted]
    card_hits = [t for t in texts if t == conv.task_card]
    # BUG #5: the card is counted, and counted exactly once.
    assert len(card_hits) == 1
    # BUG #6: no add-on re-enters the chars//4 estimator while tiktoken is
    # available — every measured string went through conv._count_text.
    assert any(t == schema_json for t in texts)
    assert schema_json in texts
    # The wire itself is measured as one string, not per-message chars//4.
    assert any(len(t) > len(conv.task_card) for t in texts)
