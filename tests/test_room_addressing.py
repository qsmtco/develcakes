"""SPEC-39 §2.1/§2.6 + SPEC-53 §1: resolve_room_addressee resolver tests.

The resolver now returns ``list[str]`` (SPEC-53): ``["all"]`` for a whole
room pick, a validated multi-select of roster keys on success, and ``[]`` on
every failure path. ``post_decision`` is monkeypatched the way
tests/test_jev_ask_tool.py does.
"""
from __future__ import annotations

from agent.llm import jev_client
from agent.runtime import AgentRuntime
from models.conversation import MessageRole
from tests.test_agent_runtime import _make_cfg

ROSTER = [("special:debugger", "Debugger"), ("special:coder", "Coder")]


def _resolver_answer(choice, confidence: float):
    return ({"addressee": {"type": "choice", "choice": choice, "confidence": confidence}}, "")


def test_resolver_confident_member_choice(monkeypatch):
    captured = {}

    def post(state, questions):
        captured["state"] = state
        captured["questions"] = questions
        return _resolver_answer("special:debugger", 0.91)

    monkeypatch.setattr("agent.llm.jev_client.post_decision", post)
    result = jev_client.resolve_room_addressee("hi debugger", ROSTER)
    assert result == ["special:debugger"]
    assert "hi debugger" in captured["state"]
    assert "Debugger" in captured["state"]
    assert "Coder" in captured["state"]
    assert "hi debugger" in jev_client._ROOM_INSTRUCTIONS
    assert captured["questions"]["addressee"]["selection"] == "multi"


def test_resolver_all_choice(monkeypatch):
    def post(state, questions):
        return _resolver_answer("all", 0.91)

    monkeypatch.setattr("agent.llm.jev_client.post_decision", post)
    result = jev_client.resolve_room_addressee("hi everyone", ROSTER)
    assert result == ["all"]


def test_resolver_multi_select_valid(monkeypatch):
    def post(state, questions):
        return _resolver_answer(["special:debugger", "special:coder"], 0.91)

    monkeypatch.setattr("agent.llm.jev_client.post_decision", post)
    result = jev_client.resolve_room_addressee("hi debugger and coder", ROSTER)
    assert result == ["special:debugger", "special:coder"]


def test_resolver_all_with_members_is_whole_room(monkeypatch):
    def post(state, questions):
        return _resolver_answer(["all", "special:debugger"], 0.91)

    monkeypatch.setattr("agent.llm.jev_client.post_decision", post)
    result = jev_client.resolve_room_addressee("hi everyone", ROSTER)
    assert result == ["all"]


def test_resolver_hallucinated_keys_dropped(monkeypatch):
    def post(state, questions):
        return _resolver_answer(["special:debugger", "special:nope"], 0.91)

    monkeypatch.setattr("agent.llm.jev_client.post_decision", post)
    result = jev_client.resolve_room_addressee("hi debugger", ROSTER)
    assert result == ["special:debugger"]


def test_resolver_duplicate_keys_deduped(monkeypatch):
    def post(state, questions):
        return _resolver_answer(["special:debugger", "special:debugger"], 0.91)

    monkeypatch.setattr("agent.llm.jev_client.post_decision", post)
    result = jev_client.resolve_room_addressee("hi debugger", ROSTER)
    assert result == ["special:debugger"]


def test_resolver_all_invalid_select_fails_closed(monkeypatch):
    def post(state, questions):
        return _resolver_answer(["special:nope", "ghost"], 0.91)

    monkeypatch.setattr("agent.llm.jev_client.post_decision", post)
    result = jev_client.resolve_room_addressee("hi debugger", ROSTER)
    assert result == []


def test_resolver_empty_select_fails_closed(monkeypatch):
    def post(state, questions):
        return _resolver_answer([], 0.91)

    monkeypatch.setattr("agent.llm.jev_client.post_decision", post)
    result = jev_client.resolve_room_addressee("hi debugger", ROSTER)
    assert result == []


def test_resolver_non_string_choice_fails_closed(monkeypatch):
    def post(state, questions):
        return _resolver_answer(7, 0.91)

    monkeypatch.setattr("agent.llm.jev_client.post_decision", post)
    result = jev_client.resolve_room_addressee("hi debugger", ROSTER)
    assert result == []


def test_resolver_none_answer_fails_closed(monkeypatch):
    def post(state, questions):
        return (None, "down")

    monkeypatch.setattr("agent.llm.jev_client.post_decision", post)
    result = jev_client.resolve_room_addressee("hi debugger", ROSTER)
    assert result == []


def test_resolver_low_confidence_fails_closed(monkeypatch):
    def post(state, questions):
        return _resolver_answer("special:debugger", 0.59)

    monkeypatch.setattr("agent.llm.jev_client.post_decision", post)
    result = jev_client.resolve_room_addressee("hi debugger", ROSTER)
    assert result == []


def test_resolver_bool_confidence_fails_closed(monkeypatch):
    def post(state, questions):
        return _resolver_answer("special:debugger", True)

    monkeypatch.setattr("agent.llm.jev_client.post_decision", post)
    result = jev_client.resolve_room_addressee("hi debugger", ROSTER)
    assert result == []


def test_resolver_unknown_choice_fails_closed(monkeypatch):
    def post(state, questions):
        return _resolver_answer("special:nope", 0.9)

    monkeypatch.setattr("agent.llm.jev_client.post_decision", post)
    result = jev_client.resolve_room_addressee("hi debugger", ROSTER)
    assert result == []


def test_resolver_empty_roster_does_not_call(monkeypatch):
    def post(state, questions):
        raise AssertionError("post_decision must not be called")

    monkeypatch.setattr("agent.llm.jev_client.post_decision", post)
    assert jev_client.resolve_room_addressee("hi", []) == []
    assert jev_client.resolve_room_addressee("hi", ()) == []
    assert jev_client.resolve_room_addressee("hi", None) == []
    assert jev_client.resolve_room_addressee(None, ROSTER) == []


# ── SPEC-39 §2.2/§2.6: note_room_line runtime queue ────────────────────────────


def _runtime_with_conv(tmp_path):
    rt = AgentRuntime(_make_cfg())
    sk = "special:debugger"
    rt.create_conversation("Debugger", sk, str(tmp_path), defer_prompt_build=True)
    return rt, sk, rt.get_conversation(sk)


def test_note_room_line_stores_when_idle(tmp_path, monkeypatch):
    saved = {"called": 0}

    def fake_save(_conv, sk):
        saved["called"] += 1
        saved["sk"] = sk

    monkeypatch.setattr("agent.runtime.save_conversation_to_disk", fake_save)
    rt, sk, conv = _runtime_with_conv(tmp_path)
    before = len(conv.messages)
    step_before = conv.step_count

    result = rt.note_room_line(sk, "[room] PM: hi debugger")

    assert result == "stored"
    assert len(conv.messages) == before + 1
    assert conv.messages[-1].role == MessageRole.USER
    assert conv.messages[-1].content == "[room] PM: hi debugger"
    assert conv.step_count == step_before
    assert saved["called"] == 1
    assert saved["sk"] == sk


def test_note_room_line_queues_while_active_then_flushes(tmp_path, monkeypatch):
    saved = {"called": 0}

    def fake_save(_conv, sk):
        saved["called"] += 1

    monkeypatch.setattr("agent.runtime.save_conversation_to_disk", fake_save)
    rt, sk, conv = _runtime_with_conv(tmp_path)
    rt._active_loops.add(sk)
    before = len(conv.messages)

    result = rt.note_room_line(sk, "[room] PM: hi debugger")

    assert result == "queued"
    assert len(conv.messages) == before
    assert rt._pending_room_lines[sk] == ["[room] PM: hi debugger"]
    assert saved["called"] == 0

    rt._flush_pending_room_lines(sk)

    assert len(conv.messages) == before + 1
    assert conv.messages[-1].content == "[room] PM: hi debugger"
    assert sk not in rt._pending_room_lines
    assert saved["called"] == 1


def test_note_room_line_save_oserror_still_stored(tmp_path, monkeypatch):
    def boom(_conv, _sk):
        raise OSError("disk full")

    monkeypatch.setattr("agent.runtime.save_conversation_to_disk", boom)
    rt, sk, conv = _runtime_with_conv(tmp_path)
    before = len(conv.messages)

    result = rt.note_room_line(sk, "[room] PM: hi debugger")

    assert result == "stored"
    assert len(conv.messages) == before + 1
    assert conv.messages[-1].content == "[room] PM: hi debugger"


def test_note_room_line_skips_blank_or_no_conversation(tmp_path, monkeypatch):
    def should_not_save(_conv, _sk):
        raise AssertionError("save must not be called for skipped lines")

    monkeypatch.setattr("agent.runtime.save_conversation_to_disk", should_not_save)
    rt, sk, conv = _runtime_with_conv(tmp_path)
    before = len(conv.messages)

    assert rt.note_room_line("", "[room] x") == "skipped"
    assert rt.note_room_line(sk, "") == "skipped"
    assert rt.note_room_line(sk, "   ") == "skipped"
    assert rt.note_room_line("ghost:missing", "[room] x") == "skipped"
    assert len(conv.messages) == before
    assert rt._pending_room_lines == {}
