"""Offline unit tests for the V6 per-member loop helpers.

Covers ``call_v6`` payload/answer handling (with ``call`` monkeypatched)
and ``derive_v6`` routing. No network access.
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest  # noqa: E402

import tools.bench_decisions as bench  # noqa: E402


def _patch_call(monkeypatch, answers, err=""):
    calls = []

    def fake_call(model, state, questions):
        calls.append((model, state, questions))
        return answers, 0.25, err

    monkeypatch.setattr(bench, "call", fake_call)
    return calls


def test_call_v6_builds_noul_questions(monkeypatch):
    """Each probe is a separate noul question; returns (choice, prob)."""
    answer = {"addr": {"type": "noul", "noul": 0.91}}
    calls = _patch_call(monkeypatch, answer)
    per_member, total, err = bench.call_v6("m", "state", bench.V6_INSTR)
    assert err == ""
    assert total == pytest.approx(0.25 * 5)
    assert list(per_member) == ["special:coder", "special:debugger",
                                "special:supervisor", "special:alphacoder",
                                "all"]
    assert per_member["special:coder"] == (True, 0.91)
    assert per_member["all"] == (True, 0.91)
    assert len(calls) == 5
    q = calls[0][2]["addr"]
    # the API has no bool type; V6 uses noul (0-1 truth probability)
    assert q["type"] == "noul"
    assert "Coder" in q["instructions"]
    assert "true" in q["criteria"] and "false" in q["criteria"]


def test_call_v6_choice_is_false_below_half(monkeypatch):
    answer = {"addr": {"type": "noul", "noul": 0.12}}
    _patch_call(monkeypatch, answer)
    per_member, _, err = bench.call_v6("m", "state", bench.V6_INSTR)
    assert err == ""
    assert per_member["special:coder"] == (False, 0.12)


def test_call_v6_propagates_error_and_malformed(monkeypatch):
    # call-level failure
    _patch_call(monkeypatch, None, "boom")
    per_member, _, err = bench.call_v6("m", "state", bench.V6_INSTR)
    assert per_member == {}
    assert err == "boom"
    # malformed answer (missing noul)
    _patch_call(monkeypatch, {"addr": {"type": "noul"}})
    per_member, _, err = bench.call_v6("m", "state", bench.V6_INSTR)
    assert per_member == {}
    assert "bad noul" in err


def test_derive_v6_routing():
    # members above threshold are picked, in roster order
    pm = {
        "special:coder": (True, 0.91),
        "special:debugger": (True, 0.87),
        "special:supervisor": (False, 0.12),
        "special:alphacoder": (False, 0.03),
        "all": (False, 0.44),
    }
    assert bench.derive_v6(pm) == ["special:coder", "special:debugger"]
    # below-threshold members do not route; all-probe then wins
    pm2 = {
        "special:coder": (True, 0.58),
        "special:debugger": (False, 0.20),
        "special:supervisor": (False, 0.10),
        "special:alphacoder": (False, 0.05),
        "all": (True, 0.97),
    }
    assert bench.derive_v6(pm2) == ["all"]
    # nothing clears → undecided
    pm3 = {
        "special:coder": (False, 0.40),
        "special:debugger": (False, 0.30),
        "special:supervisor": (False, 0.20),
        "special:alphacoder": (False, 0.10),
        "all": (True, 0.55),
    }
    assert bench.derive_v6(pm3) == []
