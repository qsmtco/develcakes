"""Runnable V6 bench harness (pytest-based because the sandbox only allows
running pytest under tests/). Produces .crabcakes/bench_v6.json with 30
rows (6 questions x 5 models) via tools.bench_decisions.call_v6, one binary
JEV question per participant (ROSTER + "all" broadcast probe)."""

import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest  # noqa: E402

from tools.bench_decisions import (  # noqa: E402
    MODELS,
    Q_CASES,
    ROSTER,
    V6_INSTR,
    call_v6,
    derive_v6,
)

OUT = ".crabcakes/bench_v6.json"

# Live harness: runs up to 150 JEV calls (5 per cell x 30 cells). Skip by
# default so CI stays offline; set RUN_V6_BENCH=1 to (re)generate the JSON.
pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_V6_BENCH"),
    reason="set RUN_V6_BENCH=1 to execute the live V6 bench",
)


def _rows_for(qname, text, expect):
    state = (
        "message:\n"
        + text
        + "\n\nparticipants:\n"
        + "\n".join(f"- {d} ({k})" for k, d in ROSTER)
    )
    out = []
    for model in MODELS:
        per_member, _, err = call_v6(model, state, V6_INSTR)
        if not per_member:
            print(f"    {model.split('/', 1)[1]:22s} ERR {err[:60]}")
            continue
        verdicts = " ".join(
            f"{k.replace('special:', '')}={ 'T' if c else 'F'}({conf:.2f})"
            for k, (c, conf) in per_member.items()
        )
        derived = derive_v6(per_member)
        correct = set(derived) == set(expect)
        confs = [conf for _, conf in per_member.values()]
        mean_conf = sum(confs) / len(confs) if confs else 0.0
        out.append({
            "question": qname,
            "model": model,
            "per_member": {
                k: {"choice": c, "confidence": conf}
                for k, (c, conf) in per_member.items()
            },
            "derived": derived,
            "expect": expect,
            "correct": correct,
            "mean_conf": round(mean_conf, 4),
        })
        print(f"    {model.split('/', 1)[1]:22s} {verdicts}")
        print(f"      derived={derived} correct={correct} mean_conf={mean_conf:.2f}")
    return out


def _load_rows():
    if os.path.exists(OUT):
        with open(OUT) as fh:
            return json.load(fh)
    return []


def _save_rows(rows):
    with open(OUT, "w") as fh:
        json.dump(rows, fh, indent=2)


@pytest.mark.parametrize("qi", range(len(Q_CASES)), ids=[q[0] for q in Q_CASES])
def test_v6_question(qi):
    qname, text, expect = Q_CASES[qi]
    print(f"\n  {qname}  → expect {sorted(e.replace('special:', '') for e in expect)}")
    rows = [r for r in _load_rows() if r["question"] != qname]
    rows.extend(_rows_for(qname, text, expect))
    _save_rows(rows)
    if qi == len(Q_CASES) - 1:
        tally = {m: {"correct": 0, "conf_sum": 0.0} for m in MODELS}
        total_correct = 0
        for r in rows:
            if r["correct"]:
                tally[r["model"]]["correct"] += 1
                tally[r["model"]]["conf_sum"] += r["mean_conf"]
                total_correct += 1
        total_cells = len(MODELS) * len(Q_CASES)
        print("\nV6 tally (correct / 30, mean confidence on correct):")
        for model in MODELS:
            t = tally[model]
            mean = t["conf_sum"] / t["correct"] if t["correct"] else 0.0
            print(f"  {model.split('/', 1)[1]:22s} {t['correct']:2d}/{total_cells}  "
                  f"mean_conf={mean:.2f}")
        print(f"  {'OVERALL':22s} {total_correct:2d}/{total_cells}")
        print(f"wrote {OUT} with {len(rows)} rows")
