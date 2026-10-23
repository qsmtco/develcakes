"""Dump bench matrix results to JSON for the HTML viewer."""
import json
import sys

sys.path.insert(0, ".")
from tools.bench_decisions import MODELS, Q_CASES, ROSTER, VARIANTS, call


def main():
    rows = []
    for vname, instr, crit in VARIANTS:
        for qname, text, expect in Q_CASES:
            state = (
                "message:\n" + text
                + "\n\nparticipants:\n"
                + "\n".join(f"- {d} ({k})" for k, d in ROSTER)
            )
            qs = {"addressee": {"type": "choice", "instructions": instr,
                                "criteria": dict(crit)}}
            for model in MODELS:
                answers, _, err = call(model, state, qs)
                a = (answers or {}).get("addressee") or {}
                rows.append({
                    "variant": vname, "question": qname, "qtext": text,
                    "expect": sorted(e.replace("special:", "") for e in expect),
                    "model": model.split("/", 1)[1],
                    "choice": str(a.get("choice")).replace("special:", ""),
                    "confidence": a.get("confidence"),
                    "probabilities": {
                        k.replace("special:", ""): v
                        for k, v in ((a.get("probabilities") or {}).items())
                    },
                    "error": err or None,
                })
    with open(".crabcakes/bench_results.json", "w") as f:
        json.dump(rows, f, indent=1)
    print(f"{len(rows)} rows written")


if __name__ == "__main__":
    main()