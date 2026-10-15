"""SPEC-30 Stage 2: classify_mask_candidates client contract.

The periodic sweep that applied these verdicts is gone. These tests cover
the client only.
"""
from __future__ import annotations

from agent.llm import jev_client


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
    verdicts = jev_client.classify_mask_candidates("state", [{"path": "p"}] * 6)
    assert calls["n"] == 1
    assert verdicts is not None and len(verdicts) == 6


def test_low_confidence_omits_that_candidate(monkeypatch):
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


def test_whole_batch_failure_returns_none(monkeypatch):
    def post(state, questions):
        return None, "decision model unavailable"

    monkeypatch.setattr(jev_client, "post_decision", post)
    assert jev_client.classify_mask_candidates("s", [{}]) is None


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
    monkeypatch.setattr(
        jev_client, "post_decision",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("called")),
    )
    assert jev_client.classify_mask_candidates(None, [{}]) is None  # type: ignore[arg-type]
