"""Offline unit tests for the V7 hybrid bench helpers.

Covers ``needs_v6`` (confidence-low, mass-split, clean) and ``call_v7``
(V2 single-ask first, V6 per-member fallback only when suspect) with
``call`` monkeypatched. No network access.
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest  # noqa: E402

import tools.bench_decisions as bench  # noqa: E402


def _v2_answer(choice, conf, probs):
    """The full API payload for a V2 single-ask (``answers``)."""
    return {"addressee": {
        "type": "choice",
        "choice": choice,
        "confidence": conf,
        "probabilities": probs,
    }}


def _v2_inner(choice, conf, probs):
    """The ``addressee`` answer object that ``needs_v6`` inspects."""
    return _v2_answer(choice, conf, probs)["addressee"]


# --- needs_v6 --------------------------------------------------------------

def test_needs_v6_conf_low_returns_true():
    """V2 confidence below V7_CONF_THRESHOLD (0.7) is suspect."""
    answer = _v2_inner("special:coder", 0.62, {
        "special:coder": 0.47,
        "special:debugger": 0.38,
        "all": 0.15,
    })
    assert bench.needs_v6(answer, "state") is True


def test_needs_v6_split_mass_returns_true():
    """High confidence is still suspect when >=2 non-all options are hot."""
    answer = _v2_inner("special:coder", 0.85, {
        "special:coder": 0.47,
        "special:debugger": 0.38,
        "all": 0.15,
    })
    assert bench.needs_v6(answer, "state") is True


def test_needs_v6_clean_returns_false():
    """High confidence with a single hot non-all option is clean."""
    answer = _v2_inner("special:coder", 0.91, {
        "special:coder": 0.94,
        "special:debugger": 0.04,
        "all": 0.02,
    })
    assert bench.needs_v6(answer, "state") is False


def test_needs_v6_clean_all_broadcast():
    """'all' with high confidence and no member split stays on V2."""
    answer = _v2_inner("all", 0.96, {
        "all": 0.96,
        "special:coder": 0.02,
        "special:debugger": 0.01,
        "special:supervisor": 0.01,
    })
    assert bench.needs_v6(answer, "state") is False


def test_needs_v6_split_threshold_strict():
    """Probability exactly at the split threshold does not count as hot."""
    answer = _v2_inner("special:coder", 0.90, {
        "special:coder": 0.40,
        "special:debugger": 0.30,  # == V7_SPLIT_THRESHOLD, not > it
        "all": 0.30,
    })
    assert bench.needs_v6(answer, "state") is False


def test_needs_v6_malformed_answer_returns_true():
    assert bench.needs_v6(None, "state") is True
    assert bench.needs_v6({}, "state") is True
    answer = _v2_inner("special:coder", "n/a", {})
    assert bench.needs_v6(answer, "state") is True


# --- call_v7 ---------------------------------------------------------------

PROBE_ANSWERS = {
    "Coder": 0.91,
    "Debugger": 0.05,
    "Supervisor": 0.03,
    "AlphaCoder": 0.02,
    "the whole room / everyone": 0.12,
}


def _patch_v7_call(monkeypatch, v2_answer):
    calls = []

    def fake_call(model, state, questions):
        calls.append((model, state, questions))
        if "addressee" in questions:
            return v2_answer, 0.2, ""
        if "addr" in questions:
            crit = questions["addr"]["criteria"]["true"]
            for label, prob in PROBE_ANSWERS.items():
                # match on the full "addressed to <label>" phrase so the
                # "Coder" entry cannot match inside "AlphaCoder"
                if f"to {label}" in crit:
                    return {"addr": {"type": "noul", "noul": prob}}, 0.2, ""
        raise AssertionError(f"unexpected call {questions}")

    monkeypatch.setattr(bench, "call", fake_call)
    return calls


def test_call_v7_clean_stays_on_v2(monkeypatch):
    v2 = _v2_answer("special:coder", 0.91, {
        "special:coder": 0.94,
        "special:debugger": 0.04,
        "all": 0.02,
    })
    calls = _patch_v7_call(monkeypatch, v2)
    derived, used_v6, notes = bench.call_v7("m", "state")
    assert derived == ["special:coder"]
    assert used_v6 is False
    assert notes.startswith("clean conf=0.91")
    # exactly one V2-style single ask, no V6 loop
    assert len(calls) == 1
    q = calls[0][2]["addressee"]
    assert q["type"] == "choice"
    assert q["instructions"] == bench.V2_INSTR


def test_call_v7_conf_low_falls_back_to_v6(monkeypatch):
    v2 = _v2_answer("special:coder", 0.62, {
        "special:coder": 0.47,
        "special:debugger": 0.38,
        "all": 0.15,
    })
    calls = _patch_v7_call(monkeypatch, v2)
    derived, used_v6, notes = bench.call_v7("m", "state")
    assert used_v6 is True
    assert derived == ["special:coder"]  # coder probe 0.91 >= 0.6
    assert "conf=0.62" in notes
    assert "→ v6" in notes
    # V2 ask + 5 V6 probes
    assert len(calls) == 6
    assert "addressee" in calls[0][2]
    assert "addr" in calls[1][2]


def test_call_v7_split_falls_back_to_v6(monkeypatch):
    v2 = _v2_answer("special:coder", 0.85, {
        "special:coder": 0.47,
        "special:debugger": 0.38,
        "all": 0.15,
    })
    calls = _patch_v7_call(monkeypatch, v2)
    derived, used_v6, notes = bench.call_v7("m", "state")
    assert used_v6 is True
    assert derived == ["special:coder"]
    assert "split" in notes and "→ v6" in notes
    assert len(calls) == 6


def test_call_v7_clean_all_stays_on_v2(monkeypatch):
    v2 = _v2_answer("all", 0.96, {
        "all": 0.96,
        "special:coder": 0.02,
        "special:debugger": 0.01,
        "special:supervisor": 0.01,
    })
    calls = _patch_v7_call(monkeypatch, v2)
    derived, used_v6, notes = bench.call_v7("m", "state")
    assert derived == ["all"]
    assert used_v6 is False
    assert len(calls) == 1


def test_call_v7_v2_error_returns_empty(monkeypatch):
    def fake_call(model, state, questions):
        return None, 0.2, "boom"

    monkeypatch.setattr(bench, "call", fake_call)
    derived, used_v6, notes = bench.call_v7("m", "state")
    assert derived == []
    assert used_v6 is False
    assert "ERR v2" in notes
