# PROPOSAL: JEV as a decision substrate for develcakes

**Date:** 2026-10-14
**Author:** Supervisor
**Status:** Proposal — brainstorm record. Not a spec, not approved, not scheduled.
**Seeds:** `agent/llm/jev_client.py` (SPEC-22), the SPEC-33 read-loop incident
**Supersedes:** nothing. **Superseded by:** nothing yet.

---

## 0. Why this document exists

The PM asked for a record of how far we could push JEV if we *wanted* to — including ideas that are half-crazy — so that we can revisit them later with a shared vocabulary. Half of this will never be built. That is the point: it is cheaper to argue with a written idea than to invent it from scratch at 2am during an incident.

Two facts frame everything below, both verified in the tree:

1. **JEV is a small, cheap, *typed* decision model.** One POST, 2.0 s timeout, returns a choice plus calibrated `probabilities` and a `confidence`. `classify_unknown` and `classify_mask_candidates` are the two existing doors.
2. **It is wired to almost nothing.** The only production callers are the mask sweep (`agent/runtime.py:3759`) and the settings probe (`probe_decision_model`). `classify_unknown` — spec'd, tested, fail-open, with a whole `FailureClass` enumeration behind it — is **called by nothing but tests**.

So the headline is not "JEV is nice." It is: **we built a decision engine and use one of its two doors, in one place, for one decision.**

---

## 1. The shape of the thing

```
post_decision(state: str, questions: dict) -> (answers | None, error)
```

Contract, and the reason it is safe to sit on hot paths:

| Condition | Result |
|---|---|
| No card / no key / no network | `None` |
| Timeout (> 2.0 s) | `None` |
| HTTP 5xx, unparseable body, wrong type, `choice` not in criteria | `None` |
| Low confidence (< 0.6) | `None` |
| Valid | `dict` of answers, each with `probabilities` + `confidence` |

**Every failure mode collapses to the same answer: no decision.** The caller picks a deterministic default. JEV is never *worse* than absent. That property is the single reason the ideas below are cheap to try — none of them can hard-fail the app.

**The four properties that make it interesting:**

1. **Calibrated confidence, not prose.** `probabilities: {"a": 0.98, "b": 0.02}` is machine-usable. A prose answer is not.
2. **Batched.** `classify_mask_candidates` sends every candidate in one call and reads back a dict. N decisions for one round-trip.
3. **Fail-open by construction** (table above).
4. **Independent.** It is not the same weights as the model asking the question. That is exactly what you want when the asking model is the thing that is confused.

---

## 2. Where it is already used, and what that proves

**The mask sweep is the proof of concept.** `classify_mask_candidates` is in the production path at `agent/runtime.py:3759`, gating a *destructive* action — rewriting a tool result into a stub. It ships with a fail-keep contract on `None` (SPEC-33). So the seam is already trusted for the highest-stakes decision we make automatically.

**It already earned its keep in the field.** During the SPEC-33 read-loop incident, the disputed `idx >= after_index` vs `<=` comparison was settled by four independent sources. JEV was the fourth, and the cheapest: one 2-second call instead of an argument. That is the template for everything in §3.

---

## 3. Ideas, roughly ordered by (value × cheapness) ÷ risk

### Tier 1 — obvious wins, days not weeks

**3.1 Wire up `classify_unknown` (it already exists!)**
The failure classifier is built, spec'd, tested, and unused. Provider 401 vs 429 vs 400 vs model-rejected is a *small closed decision problem* — the textbook use of a decision model. It should be consulted on every provider failure to decide: retry now, back off, switch model, or halt the turn and surface it. Fail-open to today's behaviour. Near-zero risk: the module, the tests, and the enum all exist; we are only adding a call site.

**3.2 A `jev_ask` tool for agents**
`provider_events` (SPEC: read-only, no approval) is the perfect template. Add:
```
jev_ask(state: str, questions: {name: {type: "choice", instructions, criteria}}) -> answers
```
Agents can then *ask a decision model a question* mid-task, with the same 2 s budget. This is the agent-facing door the PM asked about, and it is genuinely small: one tool definition, one handler, `requires_approval=False`, read-only by construction (it decides nothing by itself).

**3.3 JEV as the tie-breaker in review accept/reject**
When a Debugger audit lands and the Supervisor must decide accept vs. send-back, that decision is currently made by reading prose. Feed the diff summary + audit findings + the two options, get a calibrated choice. Fail-open to the Supervisor's own judgement — but *record the disagreement rate*. After twenty rounds we will have a dataset nobody has today.

**3.4 Which-of-N retrieval for `file_search` / `search_files`**
When a search returns 40 hits, the model currently eyeballs them and picks wrong. Batch-score the hits against the actual intent, get a ranked shortlist. Cheap, and a well-known failure mode.

### Tier 2 — genuinely interesting, needs a spec

**3.5 The stuck-loop detector that reads the transcript, not counters**
SPEC-33 diagnosed a read-loop by counting tool calls. Today that is a heuristic. The transcript is the *actual* evidence: given the last N tool calls with their arguments, JEV decides "is this agent making progress, or looping on a masked/near-dup read?" This is a judgement call over a rich state — precisely what a decision model is for, and precisely what a counter cannot do. Fail-open to today's counter threshold.

**3.6 Pre-flight spec review — a cheap rumour-check**
Before a spec reaches a Coder, ask JEV: "does this spec state its invariants, its edge cases, and its acceptance criteria? Which of these three changes is under-specified?" Not a verdict on the spec's *correctness* — a rumour-check on its *shape*. It is not Debugger, it is a smoke alarm.

**3.7 Confidence-gated escalation — "when in doubt, JEV it"**
The most interesting one in this tier, and the direct answer to the PM's ask. Give agents an explicit decision procedure in their system prompt:

> You are about to make a decision you are not certain about. State the alternatives. Call `jev_ask` with the state and the two options. If confidence is high, act. If low, escalate to the PM.

The agent does not need to know JEV is a neural network. It needs to know there is a cheap oracle. This is the mechanism that makes "agents use JEV when they have questions" real rather than aspirational — see §5 for the prompt work.

**3.8 JEV adjudicates its own failures**
If JEV fails on a mask batch, we currently warn and keep. We could ask JEV *what kind* of failure that was, and adapt. Meta, slightly unhinged, cheap. See §4.

### Tier 3 — the interesting-but-expensive shelf

**3.9 A JEV-shaped "does this test prove what it claims" check**
Feed a test's name + body + the acceptance criterion it purports to cover. Ask which acceptance criteria it actually exercises. We already got burned this session by a test that passed vacuously (the SPEC-23 single-turn fixture) — a cheap detector for that class of problem is worth real money.

**3.10 Shadow mode for the Supervisor's accept/reject**
Run JEV on every accept/reject but *do not act on it*. Six weeks of that data is the honest answer to "is the model actually good at this?" — a measured accept-rate divergence, not an opinion. Cheap to run, and it de-risks 3.3 before you trust it.

**3.11 Model-routing decisions**
Cheap decision, closed problem, big payoff: given the prompt shape and the estimated context size, which model should handle this turn? Fail-open to the configured default. This one pays for the whole program if it works.

**3.12 Batch triage: N agents, one 2-second call**
When a long implementation loop accumulates thirty audit findings, one call can rank them by "which of these is a real bug vs. a nit?" Batching is JEV's native strength and we are underusing it.

### Tier 4 — the genuinely unhinged shelf

Keep these. They are here to be argued with, not to be built.

**3.13 A JEV arbiter between Coder and Debugger deadlocks**
When Coder and Debugger disagree repeatedly, escalate to a machine arbiter with both positions, the diff, and the test evidence. Nobody likes this idea until the first three-rounds-in-a-row fight happens. Then it seems obvious.

**3.14 JEV-gated tool approval**
Replace "the human clicks approve" with "JEV rates the risk; high risk still routes to a human, low risk auto-approves." *Do not build this casually.* The enforcement layer is the security boundary, and a 2-second model call as a gate on destructive operations deserves a threat model, not a proposal. Listed because it is the obvious endpoint of the "how much can we delegate" curve, and we should know it is there on purpose rather than drift into it.

**3.15 Self-describing project state**
JEV reads `.crabcakes/workflow.md` + `context.md` and answers "what is this project actually doing right now, and what is the single most valuable next action?" A status line that argues with a stale file. Cheap to try, weird to explain.

**3.16 JEV picks the next step in the implementation loop**
The Supervisor currently plans the next Work Unit. Let JEV rank candidates by expected risk-reduction. The human still decides. This is 3.15 with a clipboard.

**3.17 The self-referential one: a JEV decision that decides whether JEV should be asked**
Recursion is a hazard, not a feature. But the *shape* is worth recording: any decision point gets a confidence threshold, and below-threshold escalates to a human. In JEV's own case, the 0.6 floor and the 2 s timeout are exactly that. Name the pattern, use it everywhere: **every automated decision in this app should be able to state its fallback.**

**3.18 Shadow JEV — a mode where JEV only ever says "I don't know"**
A model that abstains is a model you can trust. Build the abstention discipline into the culture, not just the code: *JEV saying "I don't know" at 0.3 confidence is a successful outcome, not a failure.* Cultural, not technical, and probably the most valuable item in this list.

---

## 4. Design principles for anything built on this

Distilled from the list, so each future spec inherits them:

1. **Fail-open, always.** Every call site needs a deterministic default that is today's behaviour. No call site may be able to break a turn.
2. **The fallback is part of the decision.** A spec that cannot state its fallback in one sentence is not ready.
3. **Batched, never per-item.** JEV's cost advantage is the batch. N calls for N decisions defeats the purpose.
4. **Confidence is the product.** Return the probability, not just the label. Callers should be able to threshold.
5. **Never in the enforcement path without a threat model.** §3.14 territory.
6. **Record the disagreement.** For review decisions, log what JEV said next to what the human did. Six weeks of that is the dataset.

---

## 5. Making agents use JEV on their own

The PM's second question. Three layers, in increasing order of leverage.

**Layer 1 — the tool must exist.** `jev_ask` (§3.2). Until an agent can call it, no prompt can make it happen. Note the constraint: `get_tool_definitions_for_api(conv.allowed_tools)` filters by the agent's configured allowlist (`agent/runtime.py:1917`), so any new tool must be added to the rosters or agents will not see it — a real gotcha, not a detail.

**Layer 2 — the prompt.** `build_system_prompt` (`agent/context.py:787`) composes from templates via `compose_system_prompt` (`utils/prompt_loader.py:148`). A new `prompts/system/jev-decision.md` is a *role-neutral* template — it is not coder-specific or debugger-specific, so it composes for every agent. Sketch:

```markdown
## When you are uncertain, use a decision oracle

You have a `jev_ask` tool. It submits a small, closed question to a
cheap decision model and returns a choice with a confidence score.

Use it when:
- you are choosing between two concrete actions and genuinely cannot decide;
- you have a hunch you would like checked before acting on it;
- a routine judgement call is blocking you and re-reading will not settle it.

Do NOT use it for:
- anything you can determine by reading a file or running a command — you
  have tools for that, and they are authoritative;
- confirmations of facts that are checkable; check them instead;
- stylistic or aesthetic choices that are yours to make.

How to call it well: state the situation as context, offer 2-5 concrete
options with one-line explanations, no leading phrasing. A balanced
question with no cue gets you a calibrated answer; a leading one gets
you an echo.

Reading the result: high confidence, act. Low confidence is not a
failure — it means the question was genuinely close, so escalate to the
human or pick the reversible option and say so.
```

That "balanced question, no leading phrasing" line is not decoration. §2 of the JEV dispute in this very session is the counterexample: a leading question produces a confident echo of the asker's error. We learned that the hard way, and the prompt should encode it.

**Layer 3 — teach it in the loop, not just the prompt.** The prompts/ directory already carries role and process prompts. A line in the implementation loop that says "if the implementer hits a decision you are not sure about, ask JEV" makes the habit visible to the process, not only to the model. Cheap, and it compounds.

**A caution worth recording.** A 2-second oracle on a hot path is not free — and if every agent reflexively calls it, the round-trips add up. Proposal: track call volume and hit rate alongside the product value. If `jev_ask` gets called on things a `read_file` would have settled, the prompt is too permissive.

---

## 6. Suggested sequencing

| Step | What | Why now |
|---|---|---|
| 1 | `classify_unknown` call site (§3.1) | It exists, it is tested, it is unused. Cheapest value in the document. |
| 2 | `jev_ask` tool (§3.2) | Unblocks everything in §5 and all of Tier 1. Small, read-only, no approval. |
| 3 | `jev-decision.md` prompt template (§5 Layer 2) | Only meaningful after step 2 exists. |
| 4 | Shadow mode on accept/reject (§3.10) | Measure before you act. Produces the dataset for §3.3. |
| 5 | Accept/reject for real (§3.3) | Informed by six weeks of shadow data. |
| 6 | Stuck-loop detection (§3.5) | The incident that motivated this document. Reads evidence, not counters. |
| 7 | Retrieval ranking (§3.4), spec rumour-check (§3.6) | Independent, parallelisable. |

Steps 1 and 2 are days. Everything after is a spec each.

---

## 7. Open questions for the PM

- Is a 2-second fail-open oracle acceptable on a hot path in *perceived* latency, even if the product value is high? (We should measure before we assume.)
- Should `jev_ask` be on by default for all agents, or opt-in per agent?
- Do we want JEV on the review accept/reject path at all, given it is the decision with the most human trust attached to it?
- Is the abstention culture (§3.18) something we want to state explicitly, or is that over-engineering the vibe?

---

## 8. Related documents

- `docs/specs/SPEC-22-PROVIDER-FAILURE-LIFECYCLE.md` — the decision model as specified
- `docs/specs/SPEC-33-CONTEXT-MASK-TURN-IMMUNITY.md` — the live case study
- `docs/audits/2026-10-14-CONTEXT-MASK-READ-LOOP-READ-ONLY.md` — the incident
- `agent/llm/jev_client.py` — the implementation
