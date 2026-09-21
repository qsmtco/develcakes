# Race Fix v4b — Adversarial Audit Findings

**Scope:** `ui/handlers/agent_runtime_handler.py`; audit followed `prompts/adversarialDebugger.md`.

## Verdict

**Not safe to ship.** v4b fixes the stale-completion case only if `_on_*` executes before the next turn starts. The token is captured too late: at the handler callback, not at the runtime event's originating turn. A queued stale runtime callback can capture the *new* turn's token and then pass validation.

## BUG #1 — token capture is late; stale callbacks can be relabeled as the new turn

**Severity:** CRITICAL  
**Assumption violated:** A callback's token captured in `_on_*` identifies the turn that produced the event.  
**Attack vector:** Queue a runtime callback for turn A, start turn B before that queued callback executes.  
**Reproduction:**
1. Turn A assigns `T_A` in `send_to_special_agent` (`:863-872`). Runtime queues its callback through `AgentRuntime._dispatch` (`agent/runtime.py:415-425`).
2. Before the outer idle callback invokes handler `_on_response_complete` (or `_on_error`/`_on_text_delta`), start turn B. `send_to_special_agent` replaces `_turn_tokens[sk]` with `T_B` and clears completion/ended state.
3. The stale A callback now enters `_on_response_complete`; it executes `token = self._turn_tokens.get(sk)` (`:1448`) and captures **T_B**, not T_A. It queues `_do_response_complete(..., T_B)`.
4. `_do_response_complete` compares T_B to current T_B (`:1475-1479`), accepts the stale A completion, marks B completed/ended, and renders A's text. B deltas are subsequently dropped and B's real completion is treated as duplicate.

Identical relabeling exists in `_on_text_delta` (`:994`), and `_on_error` (`:1785`), so all three tokenized paths have the same vulnerability.

**Root cause:** The runtime callback signatures contain only `session_key` and payload; v4b adds token capture after the event has already crossed the runtime queue. There is no producer-side turn token or event object carrying A's identity.  
**Fix:** Bind the token when registering/starting the turn, and have the runtime callback dispatch that token with the event; or make the runtime callback itself synchronous/turn-aware before it enters the idle queue. Do not derive stale-event identity from the mutable current `_turn_tokens` map in `_on_*`.

## BUG #2 — v4b does fix the narrower stale-completion sequence

**Severity:** none for the stated ordering  
If `_on_response_complete(A)` has already run and queued `_do_response_complete(T_A)` before B starts, then B replaces the map with T_B and the inner callback correctly rejects T_A (`:1475-1479`). The same is true for an already-captured error token. This is not sufficient for BUG #1 because the outer callback can be delayed before `_on_*` captures its token.

## BUG #3 — same-turn delta drop is fixed in the normal FIFO path

**Severity:** none for specified path  
A delta and completion that both pass through `_on_*` while the same turn is current capture the unchanged token T. Completion does not mutate `_turn_tokens`; therefore queued `_do_text_delta(T)` passes before `_do_response_complete(T)` and the v3 generation-counter drop does not recur.

**Caveat:** If a queued `_on_text_delta` runs after a new turn starts, BUG #1 relabels the old delta as the new token and it can contaminate B rather than being rejected.

## BUG #4 — un-tokenized error path before turn initialization

**Severity:** MEDIUM  
The no-project branch in `send_to_special_agent` queues/calls `_do_error` directly (`:766-773`) before assigning a turn token. Since `error_token=None` bypasses the guard (`:1818-1822`), this legacy path remains unprotected. It is normally a pre-turn validation error, but if an old queued callback shares the session it can mutate completion state without identity.  
**Fix:** Assign/resolve a turn token before any error dispatch, or make `_do_error` require a token for runtime paths and explicitly separate validation errors.

## Additional notes

- Boolean `_session_completed` correctly suppresses duplicate completion/error once the correct token reaches `_do_*`.
- Early-return ordering remains correct: token validation precedes `_ended_sessions`, and flags precede `_crh is None` (`:1481-1493`).
- `None` means “legacy caller, allow through” for all `_do_*` methods; this weakens the production invariant and should be covered or removed.
- No dedicated v4b tests were found. Required tests must delay the outer runtime callback until after a new turn starts—not only delay the inner `_do_*` callback—to catch the late-capture bug.
