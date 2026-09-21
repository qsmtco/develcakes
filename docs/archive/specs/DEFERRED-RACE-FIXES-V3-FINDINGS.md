# Deferred Race Fixes v3 — Adversarial Audit Findings

**Scope:** `ui/handlers/agent_runtime_handler.py` v3 generation, completion idempotency, and early-return changes. Audit followed `prompts/adversarialDebugger.md` and traced the actual GLib dispatch path.

## Verdict

**Not safe to ship.** Fix 3 is effective, but Fixes 1/2 have correctness flaws. In particular, the idempotency check does **not** provide idempotency, and the generation guard repeats the v1 same-turn data-loss bug under the normal two-stage dispatch.

## BUG #1
**Severity:** CRITICAL  
**Assumption violated:** A completion callback can bump the generation before all already-dispatched same-turn delta callbacks execute.  
**Attack vector:** Emit multiple runtime deltas followed by completion while `GLib` is enabled.  
**Reproduction:**
1. Runtime dispatches delta A, delta B, then completion through `AgentRuntime._dispatch` (`agent/runtime.py:415-425`), which queues outer idle callbacks.
2. `_on_text_delta` (`agent_runtime_handler.py:985-994`) queues a second idle callback with `gen=0`.
3. `_on_response_complete` queues `_do_response_complete`; that callback runs before the second-stage delta callbacks in FIFO order.
4. `_do_response_complete` (`:1473-1475`) changes generation 0→1.
5. `_do_text_delta` (`:1029-1035`) sees `delta_gen=0 < current_gen=1` and drops A and B.

**Root cause:** The generation is sampled by the outer callback but validated only by a later callback; completion can advance it between those two stages.  
**Fix:** Do not use this generation comparison to reject same-turn deltas. Capture/assign turn identity at the producer boundary, or collapse the dispatch to one main-thread callback so delta accumulation and generation ordering are atomic. Add a FIFO burst regression test asserting every same-turn delta contributes to the final text.

## BUG #2
**Severity:** HIGH  
**Assumption violated:** `(session_key, gen)` identifies a completion even after the first completion increments `gen`.  
**Attack vector:** Invoke `_do_response_complete` twice for one turn (or invoke completion then error).  
**Reproduction:**
- First `_do_response_complete`: reads gen 0, increments to 1, checks/adds `(sk, 0)`.
- Second `_do_response_complete`: reads gen 1, increments to 2, checks `(sk, 1)`, which is absent, then renders again.
- Completion followed by `_do_error` behaves the same way: error reads the newly incremented generation and is not suppressed.

**Root cause:** The key is checked after reading the current generation but the generation is incremented before the check; each duplicate observes a new key. `send_to_special_agent` only discards the current-generation key (`:866`), which does not repair this.  
**Fix:** Track a stable per-turn completion token, or check/set an ended/completed state without incrementing first. If generation remains the identity, capture the turn generation at turn start and pass it into completion/error callbacks; do not derive the key from a counter that completion itself advances.

## BUG #3
**Severity:** HIGH  
**Assumption violated:** The generation guard only rejects cross-turn stale callbacks.  
**Attack vector:** Normal burst streaming, where callbacks are dispatched via both runtime and handler idle layers.  
**Reproduction:** Same as BUG #1: all deltas capture generation 0, completion advances to 1, then all second-stage delta callbacks fail the `< current_gen` check. This is the same failure mode as v1, not merely a theoretical race.

**Actual impact:** `_streaming_text` is not accumulated and no streaming bubble starts (`_do_text_delta` returns before `:1038-1043`). The completion path may render its full `text` fallback, masking the loss in some cases; if a streaming bubble already exists or another callback path has accepted an early delta, the finalizer can expose partial text.

**Fix:** Same as BUG #1; specifically test both (a) no pre-existing bubble and (b) pre-existing streaming bubble, and assert complete output.

## BUG #4
**Severity:** LOW (memory/lifecycle)
**Assumption violated:** `_completed_turns` remains bounded and represents useful live state.  
**Attack vector:** Run many turns for one session.  
**Reproduction:** Every completion/error adds `(session_key, gen)` (`:1481`, `:1814`). New-turn cleanup discards only the current generation key (`:866`), while the prior key remains. The set grows by roughly one entry per completed turn and is never pruned globally.

**Fix:** Replace the set with one stable per-session completed-turn token/state, or prune the prior generation when starting a new turn.

## Question answers

1. **Cross-turn distinction:** The comparison is directionally correct for a delta that genuinely belongs to a prior turn (`old_gen < current_gen`), but the callback topology makes same-turn deltas look old. Therefore the overall Fix 1 is incorrect.
2. **Idempotency:** **No.** The first call increments gen before recording `(sk, gen)`; the second call reads the next gen and misses the set. Duplicate completion is rendered again.
3. **Early-return gap:** **Yes, for `_crh is None`.** `_ended_sessions.add()` and the generation increment occur before the return (`:1473-1483`); later stale deltas are rejected. `_do_error` likewise marks state before rendering.
4. **Same-turn deltas:** **Yes.** They can be dropped in the ordinary FIFO two-stage dispatch, exactly the v1 class of bug. The generation check must not be applied to a callback whose turn identity was captured before the completion callback but whose execution is deferred behind it.

## Test coverage gap

No matching generation/idempotency tests were found in `tests/`; the existing runtime tests cover callback dispatch but do not exercise this handler's two-stage FIFO ordering. Required tests: burst same-turn deltas before completion, duplicate completion, completion→error, stale prior-turn delta after a new turn, and `_crh is None` early return.
