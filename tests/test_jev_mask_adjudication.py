"""SPEC-30 Stage 2: one batched decision per mask cycle."""
from __future__ import annotations

from agent.context_strategy import DefaultContextStrategy
from agent.llm import jev_client
from models.conversation import Conversation, ToolCall


def _conv(n: int = 6) -> Conversation:
    conv = Conversation(agent_name="Coder", model="gpt-4o")
    for i in range(n):
        body = f"{i}-" + ("B" * 520)
        tc = ToolCall(
            call_id=f"c{i}",
            tool_name="read_file",
            arguments={"path": f"f{i}.py", "offset": 0},
            result=body,
        )
        conv.add_assistant_message("go", [tc])
        conv.add_tool_result(f"c{i}", body)
    return conv


def test_six_candidates_are_one_post(monkeypatch):
    calls = {"n": 0}

    def post(state, questions):
        calls["n"] += 1
        assert len(questions) == 6
        assert "B" * 520 not in state
        answers = {
            f"candidate_{i}": {"type": "choice", "choice": "mask", "confidence": 0.9}
            for i in range(6)
        }
        return answers, ""

    monkeypatch.setattr(jev_client, "post_decision", post)
    conv = _conv()
    strategy = DefaultContextStrategy()
    indices = strategy.select_maskable(conv, keep_last=0)
    assert len(indices) == 6
    verdicts = jev_client.classify_mask_candidates("state", [{"path": "p"}] * 6)
    assert calls["n"] == 1
    assert verdicts is not None and len(verdicts) == 6
    strategy.apply_mask_verdicts(conv, indices, verdicts)
    assert all(
        m.content.startswith("[masked \u2014")
        for m in conv.messages
        if m.role.value == "tool"
    )


def test_short_result_is_not_a_candidate():
    conv = Conversation(agent_name="Coder", model="gpt-4o")
    body = "tiny"
    tc = ToolCall(call_id="c0", tool_name="read_file", arguments={"path": "s.py"}, result=body)
    conv.add_assistant_message("go", [tc])
    conv.add_tool_result("c0", body)
    assert DefaultContextStrategy().select_maskable(conv, keep_last=0) == []


def test_low_confidence_masks_that_candidate_only(monkeypatch):
    def post(state, questions):
        answers = {}
        for i in range(6):
            conf = 0.4 if i == 1 else 0.95
            choice = "keep" if i != 1 else "mask"
            answers[f"candidate_{i}"] = {"type": "choice", "choice": choice, "confidence": conf}
        return answers, ""

    monkeypatch.setattr(jev_client, "post_decision", post)
    verdicts = jev_client.classify_mask_candidates("s", [{}] * 6)
    assert verdicts is not None
    assert "candidate_1" not in verdicts
    assert verdicts["candidate_0"] == "keep"
    conv = _conv()
    indices = DefaultContextStrategy().select_maskable(conv, keep_last=0)
    DefaultContextStrategy().apply_mask_verdicts(conv, indices, verdicts)
    tools = [m.content for m in conv.messages if m.role.value == "tool"]
    # select_maskable walks newest-first, so candidate_0 is the last tool result.
    assert tools[-1].startswith("5-")
    assert tools[-2].startswith("[masked \u2014")


def test_whole_batch_failure_masks_all(monkeypatch):
    def post(state, questions):
        return None, "decision model unavailable"

    monkeypatch.setattr(jev_client, "post_decision", post)
    assert jev_client.classify_mask_candidates("s", [{}]) is None
    conv = _conv(2)
    strategy = DefaultContextStrategy()
    indices = strategy.select_maskable(conv, keep_last=0)
    strategy.apply_mask_verdicts(conv, indices, None)
    assert all(
        m.content.startswith("[masked \u2014")
        for m in conv.messages
        if m.role.value == "tool"
    )


def test_summarize_keeps_prefix_and_first_200_chars():
    conv = _conv(1)
    original = [m.content for m in conv.messages if m.role.value == "tool"][0]
    strategy = DefaultContextStrategy()
    indices = strategy.select_maskable(conv, keep_last=0)
    strategy.apply_mask_verdicts(conv, indices, {"candidate_0": "summarize"})
    text = [m.content for m in conv.messages if m.role.value == "tool"][0]
    assert text.startswith("[masked \u2014")
    assert text.split("\n", 1)[1] == original[:200]
    assert "model-gist" not in text


def test_empty_candidates_do_not_post(monkeypatch):
    def post(state, questions):
        raise AssertionError("post_decision should not run")

    monkeypatch.setattr(jev_client, "post_decision", post)
    assert jev_client.classify_mask_candidates("s", []) == {}


def test_garbage_answer_is_omitted(monkeypatch):
    def post(state, questions):
        return {
            "candidate_0": {"type": "text", "choice": "keep", "confidence": 0.9},
            "candidate_1": {"type": "choice", "choice": "keep", "confidence": True},
            "candidate_2": "nope",
        }, ""

    monkeypatch.setattr(jev_client, "post_decision", post)
    verdicts = jev_client.classify_mask_candidates("s", [{}, {}, {}])
    assert verdicts == {}


def test_bad_state_does_not_raise(monkeypatch):
    monkeypatch.setattr(jev_client, "post_decision", lambda *a, **k: (_ for _ in ()).throw(AssertionError("called")))
    assert jev_client.classify_mask_candidates(None, [{}]) is None  # type: ignore[arg-type]
