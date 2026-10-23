#!/usr/bin/env python3
"""Thorough JEV decision-model bench.

Matrix: 5 payload/instruction variants x 6 questions x 5 models.
Prints the full `probabilities` map for every cell so we can see how
each model spreads mass, not just the top pick.
Run: .venv/bin/python tools/bench_decisions.py
"""
import json
import os
import re
import time
import urllib.request

ENDPOINT = "https://openrouter.ai/api/v1/systemone"
# Never hardcode the key: read it from the environment (OPENROUTER_API_KEY)
# or the provider store, exactly like agent/llm/jev_client.py does.
KEY = os.environ.get("OPENROUTER_API_KEY", "")
if not KEY:
    # Fall back to the DecisionModel card in the provider store, if present.
    try:
        from utils.providers_store import load_providers

        for _cfg in load_providers():
            if getattr(_cfg, "default_model", "") and getattr(_cfg, "api_key", ""):
                KEY = _cfg.api_key.strip()
                ENDPOINT = (getattr(_cfg, "base_url", "") or ENDPOINT).rstrip("/")
                break
    except Exception:  # noqa: BLE001,S110 — offline-safe fallback, KEY stays empty
        pass
if not KEY:
    raise SystemExit(
        "bench_decisions needs OPENROUTER_API_KEY (or a DecisionModel provider "
        "card with an api_key); refusing to run keyless"
    )
MODELS = [
    "typesafe/jev-1.13",
    "typesafe/jev-1.14",
    "baseten/mammoth-1:free",
    "baseten/elephant-1:free",
    "perplexity/pplx-decider-v1.2",
]
TIMEOUT = 10.0
TOP_N = 5  # show top 5 probabilities per cell

ROSTER = [
    ("special:coder", "Coder"),
    ("special:debugger", "Debugger"),
    ("special:supervisor", "Supervisor"),
    ("special:alphacoder", "AlphaCoder"),
]

# ---- room criteria maps (shared) -------------------------------------------
ROOM_CRIT = {
    "all": 'Only an explicit room-wide greeting ("everyone", "team", '
           '"hey room"), or a question that names no participant.',
    "special:coder": "The message addresses Coder (special:coder).",
    "special:debugger": "The message addresses Debugger (special:debugger).",
    "special:supervisor": "The message addresses Supervisor (special:supervisor).",
    "special:alphacoder": "The message addresses AlphaCoder (special:alphacoder).",
}
ROOM_CRIT_MULTI = dict(ROOM_CRIT)
ROOM_CRIT_MULTI["all"] = (
    "LAST RESORT ONLY: a question that names NO participant and is not a "
    "greeting. If any participant is named, addressed, or asked about, "
    "do NOT pick this."
)
for _k, _d in ROSTER:
    ROOM_CRIT_MULTI[_k] = (
        f"{_d} ({_k}) — pick this when the message names, addresses, or asks "
        f"about {_d}, including in passing. Multiple picks are fine."
    )

# ---- 5 payload variants ----------------------------------------------------
# Each variant: (name, questions-dict builder, state builder)
V1_INSTR = (
    "The human just spoke in a group chat. Decide who should answer. "
    "Choose the participant(s) the message addresses — including when a "
    "participant is merely mentioned as the subject of a question. "
    "Prefer a named participant over 'all'. Choose 'all' only for an "
    "explicit room-wide greeting or a question that names no participant."
)
V2_INSTR = (
    "Group chat routing. Which participant(s) should reply to this message? "
    "Rules: (1) a message naming a participant — even indirectly, like "
    "'supervisor app restarted, what should I test' — routes to that "
    "participant. (2) A message naming several participants routes to all "
    "of them. (3) 'all' is ONLY for greetings to the whole room "
    "('hey everyone', 'team') or questions naming nobody. "
    "Multiple answers are normal and expected."
)
V3_INSTR = (
    "Who is this message FOR? Answer with the addressee(s). Direct address "
    "('hi debugger'), indirect reference ('the supervisor's restart'), and "
    "questions about someone ('alphacoder why did the test fail') all count "
    "as addressing that participant. 'all' means the message is for the "
    "whole room — a broadcast greeting or a question nobody in particular."
)
V4_INSTR = (
    "You are a chat router. Score each participant option by how strongly "
    "this message is addressed to them. If the message addresses more than "
    "one participant, give each addressed participant high probability. "
    "'all' should get probability only when the message is a room-wide "
    "broadcast. Indirect mentions count as address."
)
V5_INSTR = (
    "Route this group-chat message. Pick every participant the message is "
    "addressed to. If the message is about a specific participant, that "
    "participant is addressed, even without a greeting. 'all' is reserved "
    "for true broadcasts: greetings to everyone, or open questions with no "
    "named participant."
)

VARIANTS = [
    ("V1 baseline (current prod)", V1_INSTR, ROOM_CRIT),
    ("V2 rules-list", V2_INSTR, ROOM_CRIT),
    ("V3 'who is this FOR'", V3_INSTR, ROOM_CRIT),
    ("V4 score-each", V4_INSTR, ROOM_CRIT_MULTI),
    ("V5 crisp router", V5_INSTR, ROOM_CRIT_MULTI),
]

# ---- 6 questions -----------------------------------------------------------
Q_CASES = [
    ("Q1 multi2 'hi debugger and coder'", "hi debugger and coder",
     ["special:debugger", "special:coder"]),
    ("Q2 indirect 'supervisor app restarted…'", "supervisor app restarted what should i test first",
     ["special:supervisor"]),
    ("Q3 broadcast 'hey everyone…'", "hey everyone check out the new build", ["all"]),
    ("Q4 open 'what do you all think…'", "what do you all think of the new spec", ["all"]),
    ("Q5 single 'coder fix the login bug'", "coder can you fix the login bug in auth.py",
     ["special:coder"]),
    ("Q6 no-name 'the pytest run segfaulted'", "guys the pytest run segfaulted", ["all"]),
]

# ---- V6 per-member loop ----------------------------------------------------
V6_THRESHOLD = 0.6
V6_INSTR = (
    "Is this group-chat message addressed to {member}? Reply true or false. "
    "Direct address, indirect reference, and questions about {member} all "
    "count as addressing. Be strict: if the message is not about {member}, "
    "answer false."
)

# ---- V7 hybrid (V2 single-ask + V6 per-member fallback) --------------------
V7_CONF_THRESHOLD = 0.7
V7_SPLIT_THRESHOLD = 0.3


def call(model, state, questions):
    body = json.dumps({"model": model, "state": state, "questions": questions}).encode()
    req = urllib.request.Request(
        ENDPOINT,
        data=body,
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            payload = json.loads(r.read().decode("utf-8", errors="replace"))
        return payload.get("answers"), time.monotonic() - t0, ""
    except Exception as exc:  # noqa: BLE001 — bench reports, never crashes
        return None, time.monotonic() - t0, f"{type(exc).__name__}: {exc}"


def fmt_probs(answer):
    probs = (answer or {}).get("probabilities") or {}
    ranked = sorted(probs.items(), key=lambda kv: -kv[1])[:TOP_N]
    return " ".join(f"{k.replace('special:', '')}={v:.2f}" for k, v in ranked)


def main():
    for vname, instr, crit in VARIANTS:
        print(f"\n### {vname}")
        print("    instructions:", instr[:90].replace("\n", " "), "…")
        for qname, text, expect in Q_CASES:
            state = (
                "message:\n" + text
                + "\n\nparticipants:\n"
                + "\n".join(f"- {d} ({k})" for k, d in ROSTER)
            )
            qs = {"addressee": {"type": "choice", "instructions": instr,
                                "criteria": dict(crit)}}
            print(f"  {qname}  → expect {sorted(e.replace('special:', '') for e in expect)}")
            for model in MODELS:
                answers, _, err = call(model, state, qs)
                if answers is None:
                    print(f"    {model:32s} ERR {err[:60]}")
                    continue
                a = answers.get("addressee") or {}
                top = str(a.get("choice"))
                conf = a.get("confidence")
                print(f"    {model.split('/', 1)[1]:22s} conf={conf!s:>8s} "
                      f"top={top.replace('special:', '')!s:<14s} {fmt_probs(a)}")
    print("\ndone.")


def call_v6(model, state, instructions):
    """One binary question per roster member plus an 'all' broadcast probe.

    Uses the JEV ``noul`` question type (the API has no bool type): each
    call returns a 0–1 probability that the statement is true. We record
    ``(choice, prob)`` with ``choice = prob >= 0.5`` and ``prob`` standing in
    for confidence, so the verdict line reads ``coder=T(0.91)``.

    Returns ``(per_member, total_elapsed, error)`` where ``per_member`` maps
    each roster key and ``"all"`` to ``(choice_bool, prob)``. Any failed or
    malformed call fails the whole cell (mirrors ``call``).
    """
    probes = list(ROSTER) + [("all", "the whole room / everyone")]
    per_member = {}
    total = 0.0
    for key, label in probes:
        instr = instructions.format(member=label)
        qs = {
            "addr": {
                "type": "noul",
                "instructions": instr,
                "criteria": {
                    "true": f"The message is addressed to {label}.",
                    "false": f"The message is NOT about {label}.",
                },
            }
        }
        answers, elapsed, err = call(model, state, qs)
        total += elapsed
        if answers is None:
            return {}, total, err
        a = answers.get("addr") or {}
        prob = a.get("noul")
        if not isinstance(prob, (int, float)) or isinstance(prob, bool):
            return {}, total, f"bad noul {prob!r}"
        prob = float(prob)
        per_member[key] = (prob >= 0.5, prob)
    return per_member, total, ""


def derive_v6(per_member):
    """Derive routing from per-member bool verdicts.

    Every member whose True confidence clears ``V6_THRESHOLD``; else
    ``["all"]`` if the broadcast probe clears threshold; else ``[]``.
    """
    picks = [
        key for key, (choice, conf) in per_member.items()
        if key != "all" and choice and conf >= V6_THRESHOLD
    ]
    if picks:
        return picks
    all_choice, all_conf = per_member.get("all", (False, 0.0))
    if all_choice and all_conf >= V6_THRESHOLD:
        return ["all"]
    return []


def main_v6():
    rows = []
    tally = {model: {"correct": 0, "conf_sum": 0.0} for model in MODELS}
    total_correct = 0
    total_cells = len(Q_CASES) * len(MODELS)
    print("\n### V6 per-member loop (one binary question per participant)")
    for qname, text, expect in Q_CASES:
        state = (
            "message:\n" + text
            + "\n\nparticipants:\n"
            + "\n".join(f"- {d} ({k})" for k, d in ROSTER)
        )
        print(f"  {qname}  → expect {sorted(e.replace('special:', '') for e in expect)}")
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
            if correct:
                tally[model]["correct"] += 1
                tally[model]["conf_sum"] += mean_conf
                total_correct += 1
            rows.append({
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
    print("\nV6 tally (correct / 30, mean confidence on correct):")
    for model in MODELS:
        t = tally[model]
        mean = t["conf_sum"] / t["correct"] if t["correct"] else 0.0
        print(f"  {model.split('/', 1)[1]:22s} {t['correct']:2d}/{total_cells}  "
              f"mean_conf={mean:.2f}")
    print(f"  {'OVERALL':22s} {total_correct:2d}/{total_cells}")
    with open(".crabcakes/bench_v6.json", "w") as fh:
        json.dump(rows, fh, indent=2)
    print(f"wrote .crabcakes/bench_v6.json with {len(rows)} rows")


def needs_v6(answer, state):
    """True when the V2 single-ask looks suspect and V6 should disambiguate.

    Two red flags:
    * V2 ``confidence`` below ``V7_CONF_THRESHOLD`` (0.7) — the model is not
      sure enough about its single choice;
    * at least two non-``all`` options carry probability above
      ``V7_SPLIT_THRESHOLD`` (0.3) — mass split between members, i.e. a
      possible multi-addressee (Q1) even if the top choice won.
    """
    del state  # reserved for future use; the decision only needs the answer.
    if not isinstance(answer, dict):
        return True
    conf = answer.get("confidence")
    if not isinstance(conf, (int, float)) or isinstance(conf, bool):
        return True
    if conf < V7_CONF_THRESHOLD:
        return True
    probs = answer.get("probabilities") or {}
    hot = sum(
        1 for k, v in probs.items()
        if k != "all" and isinstance(v, (int, float))
        and not isinstance(v, bool) and v > V7_SPLIT_THRESHOLD
    )
    return hot >= 2


def call_v7(model, state):
    """V7 hybrid: V2 single-ask first, per-member V6 loop only when suspect.

    Returns ``(derived, used_v6, notes)``. ``derived`` is the final routing
    set (a V2 choice, ``["all"]``, or the V6-derived set) and ``notes`` is a
    one-line explanation for the printout.
    """
    qs = {
        "addressee": {
            "type": "choice",
            "instructions": V2_INSTR,
            "criteria": dict(ROOM_CRIT),
        }
    }
    answers, _, v2_err = call(model, state, qs)
    if answers is None:
        return [], False, f"ERR v2: {v2_err[:60]}"
    answer = answers.get("addressee") or {}
    v2_choice = answer.get("choice")
    v2_conf = answer.get("confidence")
    if not isinstance(v2_conf, (int, float)) or isinstance(v2_conf, bool):
        v2_conf = 0.0
    probs = answer.get("probabilities") or {}

    if not needs_v6(answer, state):
        derived = [v2_choice] if v2_choice else []
        return derived, False, f"clean conf={v2_conf:.2f}"

    split = [
        f"{k.replace('special:', '')}={v:.2f}"
        for k, v in sorted(probs.items(), key=lambda kv: -kv[1])
        if k != "all" and isinstance(v, (int, float))
        and not isinstance(v, bool) and v > V7_SPLIT_THRESHOLD
    ]
    if len(split) >= 2:
        note = f"conf={v2_conf:.2f} split {' '.join(split)} → v6"
    else:
        note = f"conf={v2_conf:.2f}<0.7 → v6"

    per_member, _, v6_err = call_v6(model, state, V6_INSTR)
    if not per_member:
        return [], True, f"ERR v6: {v6_err[:60]}"
    return derive_v6(per_member), True, note


def main_v7():
    rows = []
    tally = {model: {"correct": 0} for model in MODELS}
    total_correct = 0
    total_cells = len(Q_CASES) * len(MODELS)
    fallbacks = {model: 0 for model in MODELS}
    fallback_qs = {}
    v2_clean_conf = []
    print("\n### V7 hybrid (V2 single-ask + V6 per-member fallback)")
    for qname, text, expect in Q_CASES:
        state = (
            "message:\n" + text
            + "\n\nparticipants:\n"
            + "\n".join(f"- {d} ({k})" for k, d in ROSTER)
        )
        print(f"  {qname}  → expect {sorted(e.replace('special:', '') for e in expect)}")
        for model in MODELS:
            derived, used_v6, notes = call_v7(model, state)
            m_conf = re.search(r"conf=([\d.]+)", notes)
            v2_conf = float(m_conf.group(1)) if m_conf else None
            m_choice = re.search(r"\b(all|special:\w+)\b", notes)
            v2_choice = m_choice.group(1) if m_choice else None
            correct = set(derived) == set(expect)
            if correct:
                tally[model]["correct"] += 1
                total_correct += 1
            rows.append({
                "question": qname,
                "model": model,
                "v2_choice": v2_choice,
                "v2_conf": v2_conf,
                "used_v6": used_v6,
                "derived": derived,
                "expect": expect,
                "correct": correct,
                "notes": notes,
            })
            if used_v6:
                fallbacks[model] += 1
                fallback_qs.setdefault(qname, 0)
                fallback_qs[qname] += 1
            elif v2_conf is not None:
                v2_clean_conf.append(v2_conf)
            print(f"    {model.split('/', 1)[1]:22s} {notes}")
            print(f"      v2={v2_choice} conf={v2_conf} → v6? "
                  f"{'yes' if used_v6 else 'no'}  "
                  f"derived={derived} correct={correct}")
    print("\nV7 tally (correct / 30):")
    for model in MODELS:
        t = tally[model]
        print(f"  {model.split('/', 1)[1]:22s} {t['correct']:2d}/{total_cells}  "
              f"fallbacks={fallbacks[model]}")
    print(f"  {'OVERALL':22s} {total_correct:2d}/{total_cells}")
    print("\nV7 fallbacks by question:")
    for qname, _text, _expect in Q_CASES:
        print(f"  {qname:28s} {fallback_qs.get(qname, 0)}/5 fell back to V6")
    if v2_clean_conf:
        print(f"  mean V2 conf on non-fallback cells = {sum(v2_clean_conf) / len(v2_clean_conf):.2f}")
    with open(".crabcakes/bench_v7.json", "w") as fh:
        json.dump(rows, fh, indent=2)
    print(f"wrote .crabcakes/bench_v7.json with {len(rows)} rows")


if __name__ == "__main__":
    main()
    main_v6()
    main_v7()