# Race Fix v4c — Adversarial Audit Findings

**Scope:** `agent/runtime.py` `_dispatch` and `ui/handlers/agent_runtime_handler.py` token flow. Audit followed `prompts/adversarialDebugger.md`.

## Verdict

**Not safe to ship.** v4c fixes v4b's late capture only if the runtime's `_turn_token` is immutable for the lifetime of the producing loop. It is not: the same `AgentRuntime` is reused and `rt._turn_token` is overwritten for each turn. A delayed old loop can therefore capture the new token at `_dispatch` time. The TypeError fallback is also unsafe.

## BUG #1 — dispatch-time capture is still too late across concurrent turns

**Severity:** CRITICAL  
**Assumption violated:** `rt._turn_token` is stable for the entire turn.  
**Attack vector:** Start turn A, then start turn B before A's background loop has emitted all callbacks.  
**Reproduction:**
1. A assigns `rt._turn_token = T_A` (`agent_runtime_handler.py:874-878`) and starts A's thread.
2. B starts before A finishes; the same runtime object is reused and assigns `rt._turn_token = T_B`.
3. A's background `_run_loop` later calls `_dispatch` for a stale delta/completion/error. `_dispatch` reads the mutable field at `runtime.py:426`, so it captures **T_B**, not T_A.
4. The handler compares that token with current T_B and accepts the stale A event. A completion can end B, while A deltas can contaminate B.

Thus producer-side capture fixes only the v4b window between `_dispatch` and the handler idle callback; it does not establish producer-turn identity.

**Fix:** Bind a turn token to the `_run_loop` invocation (pass it as a thread argument) and pass that immutable local token to every `_dispatch` call, or maintain per-turn runtime instances. `_dispatch` must receive the token explicitly; it must not read mutable runtime state.

## BUG #2 — TypeError fallback can mask real callback failures and duplicate side effects

**Severity:** HIGH  
**Assumption violated:** A `TypeError` raised by `callback(*args, **kwargs, _turn_token=token)` means only that the callback rejects the keyword.  
**Attack vector:** A token-aware callback accepts `_turn_token` but raises `TypeError` internally; or a legacy callback performs side effects and then raises `TypeError`.  
**Reproduction:**
```python
def callback(session, text, _turn_token=None):
    record.append(text)
    raise TypeError("bug inside callback")
```
`inner` catches that TypeError (`runtime.py:428-432`) and calls the callback a second time without `_turn_token`, duplicating `record` and potentially duplicating UI/state mutations. If the second call raises, it escapes `inner` rather than reaching the following `except Exception` clause, so GLib may silently swallow it.

**Fix:** Do not use exception handling for signature detection. Pass tokens only to the three known token-aware callbacks, or inspect the callable signature/adapter at registration. If compatibility fallback is unavoidable, distinguish unexpected callback TypeErrors from argument-binding TypeErrors and ensure the callback is never retried after it has begun execution.

## BUG #3 — un-tokenized validation-error path remains

**Severity:** MEDIUM  
The no-project branch directly queues/calls `_do_error` (`agent_runtime_handler.py:766-773`) without a token. `_do_error` allows `error_token=None`, bypassing stale-event validation. This is a separate pre-turn path, but it weakens the all-callback token invariant and can mutate completion state if mixed with pending callbacks for the same session.

**Fix:** Give validation errors an explicit current-turn token, or separate them from turn completion state; require tokens for runtime error callbacks.

## Confirmed fixes

- Same-turn delta dropping from v3 is fixed in the ordinary FIFO path: token does not change at completion.
- A token already captured by `_dispatch` before a new turn starts is correctly rejected by `_do_*` after the new token is installed.
- Boolean completion tracking prevents duplicate completion once the correct token reaches `_do_response_complete`/`_do_error`.
- Flags are set before `_crh is None`, preserving the early-return fix.

## Test coverage gap

No dedicated v4c tests were found. Required tests: delayed old background loop after a new turn (including delta, completion, and error), callback-internal TypeError with side-effect count, legacy callback TypeError, FIFO same-turn burst, duplicate completion, and `_crh is None`.
