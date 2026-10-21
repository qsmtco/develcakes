# SPEC-46: JEV wiring — decision prompt + failure classifier call site

**Date:** 2026-10-21
**Author:** Supervisor
**Status:** Implemented 2026-10-21 — two fix rounds (Debugger audits), final read1 fix spot-checked by Supervisor. Uncommitted, awaiting PM accept.
**Seeds:** `docs/proposals/PROPOSAL-jev-decision-substrate.md` §3.1, §5 Layer 2
**Related:** SPEC-22 (failure lifecycle), SPEC-25 (repo-map rerank), SPEC-29 (turn extension), SPEC-43 (coder harness)
**Supersedes:** none. **Superseded by:** nothing yet.

---

## 0. Problem

The JEV decision model has production doors — the SPEC-33 mask sweep, the SPEC-25 reranker, and the `jev_ask` agent tool. Two cheap, spec'd, tested pieces remain unwired:

1. `agent/llm/jev_client.py::classify_unknown` — spec'd in SPEC-22, fully tested, called by nothing but tests.
2. The `jev-decision.md` role-neutral prompt template — the discipline layer for `jev_ask`. The tool shipped without it; agents get zero guidance on when to use it, and the proposal's hardest-won lesson (balanced phrasing, no leading questions) lives only in the proposal doc.

## 1. Goals

1. Open the `classify_unknown` door: consult the decision model on provider failures the deterministic classifier marks `UNKNOWN`, so retry/failover decisions use calibrated evidence instead of a fallback guess.
2. Ship the discipline layer: a `prompts/system/jev-decision.md` composed into every non-coder agent system prompt, teaching agents when to use `jev_ask` — and when not to.

## 2. Design

### 2.1 `classify_unknown` call site (`agent/runtime.py`)

**Where:** JEV refinement must run in **all three `ProviderFailure` producers**, because `decide()` consumes failures from all of them:

1. `AgentRuntime._classified_stream_error` (`agent/runtime.py:1084`) — produces `metadata["provider_failure"]` for the no-failover path (`runtime.py:2175`, `2222`).
2. `AgentRuntime._stream_failure_exception` (`agent/runtime.py:3063`) — the mid-stream/empty-content path; its failure feeds `_apply_chain_decision` → `decide()` (`runtime.py:2927`). **With failover on (the default), this is the path that matters.**
3. `AgentRuntime._attach_provider_failure` (`agent/runtime.py:1029`) — the raised-exception / request-boundary path; also feeds `decide()`.

*(Correction 2026-10-21: the original draft named `_classified_stream_error` as the single choke point. Debugger audit proved that wrong — it feeds only `TurnResult.metadata`, so a refinement there alone changes a label, not a policy. This section now names all three producers.)*

**How:** one helper — `_jev_refine_unknown(failure)` — holding the entire SPEC-46 logic (UNKNOWN-only check, `classify_unknown` call, validated-answer replace, fail-open try/except), called by each producer after it builds its failure. One call per failure is preserved because every failure passes through exactly one producer.

**What:** After `failure_for_stream(...)` returns a `ProviderFailure` with `failure_class == UNKNOWN` — and only then — ask the decision model:

```
state = "Provider stream failure. provider={provider} model={model}
         status={status_code} detail={sanitized, 400-char cap}"
questions = {
    "failure_class": {
        "type": "choice",
        "instructions": "Which failure class best fits this provider error?",
        "criteria": ["transient", "permanent_auth", "permanent_model",
                     "bad_request", "unknown"],
        "options": {  # one-line meaning per class, from _CLASS_LABEL semantics
            "transient": "retryable: timeout, 5xx, connection",
            ...
        }
    }
}
```

**Decision use:** if JEV answers with high confidence (≥ 0.6 — the validation `classify_unknown` already performs), replace `failure.failure_class` with the answered class before the failure enters `metadata["provider_failure"]` and the policy layer sees it. If JEV returns `None` — no card, timeout, low confidence, malformed — keep `UNKNOWN` and proceed exactly as today.

### 2.2 `jev-decision.md` prompt template (new file)

Role-neutral template, modeled on the proposal §5 Layer 2 sketch:

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

Draft wording is indicative; final wording is settled at implementation review. Two optional additions, per the proposal: the two-plus-one rule ("would a `read_file` settle this? if yes, read the file") and an abstention-culture line ("an oracle that can abstain is an oracle you can trust") — see §6.

### 2.3 Composition

- `compose_system_prompt` (`utils/prompt_loader.py:148`): add `jev-decision` to the **Identity (always)** group, after `collab.md`, before `crabcakes-context.md`. A missing template is silently skipped — free backward compatibility.
- Coder harness (SPEC-43): **excluded.** The coder prompt is `coder.md` only; `jev-decision.md` is not composed into it.

## 3. Invariants

1. **Fail-open, always.** JEV returning `None` must leave runtime behaviour byte-identical to today: `UNKNOWN` class, existing policy path, no new exceptions, no new UI warnings.
2. **UNKNOWN-only refinement.** JEV may only refine `UNKNOWN` → a specific class. It may never overwrite a deterministically classified failure (`TRANSIENT`, `PERMANENT_AUTH`, `PERMANENT_MODEL`, `BAD_REQUEST`) and never reclassify downward to `UNKNOWN`.
3. **2-second ceiling.** One call per failure, `post_decision`'s 2.0 s timeout is the ceiling, no retry. The call sits on the error path only — the turn has already failed, so this adds no latency to healthy turns.
4. **No new surface.** `jev_ask` already exists; this spec adds no tools, no UI, no cards.
5. **Coder prompt untouched** (SPEC-43 minimalism).

## 4. Acceptance criteria

1. Simulated UNKNOWN stream failure + reachable card answering high-confidence → `metadata["provider_failure"].failure_class` is the JEV-refined class.
2. Same failure + no card / timeout / low-confidence answer → class stays `UNKNOWN`; the turn proceeds exactly as today.
3. Deterministic classes never pass through JEV — provable by asserting the JEV client is not invoked when class != `UNKNOWN`.
4. `compose_system_prompt` output for non-coder roles contains the jev-decision block; coder output does not.
5. **The refined class must reach `decide()`.** On the `_stream_failure_exception` and `_attach_provider_failure` paths, a high-confidence JEV refinement (`UNKNOWN`→`TRANSIENT`) must change the resulting policy outcome vs. an unrefined `UNKNOWN` (retry/failover where `UNKNOWN` would halt). *(Added by audit 2026-10-21.)*
6. **Mutant kill requirement.** Removing the UNKNOWN-only guard, or removing the `refined != UNKNOWN` check, must each cause at least one test failure. *(Added by audit 2026-10-21, after both mutants survived the v1 tests.)*
7. Full test suite green.

## 5. Non-goals

- Stuck-loop detection (proposal §3.5) — future spec.
- Shadow mode / accept-reject tie-breaking (proposal §3.3 / §3.10) — future spec.
- Any JEV use in the enforcement path (proposal §3.14) — explicitly out.
- Changing `jev_ask`'s signature or roster membership.
- The work-unit system. Per PM direction (2026-10-21), this spec is tracked spec-first; scheduling happens outside the work-unit system for now (to be revisited separately).

## 6. Decisions (settled 2026-10-21, PM delegated via "please proceed")

1. **The refined class flows into policy.** Refinement happens in `_classified_stream_error` before the failure reaches `metadata["provider_failure"]`, so `decide()` (`agent/llm/failure_policy.py:31`) sees the refined class naturally: JEV→`TRANSIENT` earns the retry budget + failover that `UNKNOWN` (single retry, halt, no failover — `failure_policy.py:94-108`) denies. Label-only was rejected: the class field exists to drive `decide()`, and keeping two classes apart would be double bookkeeping with zero upside. Fail-open preserves today's behaviour when JEV abstains.
2. **Template includes the two additions** — the two-plus-one rule and the abstention-culture line. Three lines of text, high leverage, both straight from the proposal (§3.18, §5).

## 7. Implementation sketch

Files touched: `agent/runtime.py` (call site in `_classified_stream_error`), `prompts/system/jev-decision.md` (new), `utils/prompt_loader.py` (one composition entry). Roughly a day of Coder time.

## 8. Testing notes

Existing coverage: `tests/test_failure_classifier.py` (classifier), JEV client/probe tests in the suite. New tests sit beside them. Follow the project's mutant-verification style (SPEC-44 precedent) where cheap.
