# Race Fix v4 — Adversarial Audit Findings

**Scope:** `ui/handlers/agent_runtime_handler.py`; audit prompt: `prompts/adversarialDebugger.md`.

## Verdict

**Partially fixes v3, but not safe to ship.** The turn token fixes v3's generation-counter same-turn drop in the stated FIFO ordering, and the completion boolean fixes duplicate completion within one turn. However, completion/error callbacks have no turn token, leaving a cross-turn stale-completion race that can terminate the wrong (new) turn.

## BUG #1 — stale completion can complete a new turn

**Severity:** CRITICAL  
**Assumption violated:** A completion/error callback belongs to the turn that is current when it executes.  
**Attack vector:** Finish turn A, immediately start turn B, while A's queued completion (or error) callback is still pending.  
**Reproduction:**
1. `send_to_special_agent(A)` assigns token `T_A` (`:863-872`).
2. Runtime queues A's completion callback, but it has not yet run.
3. User starts B; `send_to_special_agent(B)` clears `_session_completed`, clears `_ended_sessions`, and replaces `_turn_tokens[sk]` with `T_B`.
4. A's `_do_response_complete(sk, text_A)` then runs. It has no captured token, so it cannot identify A; it adds `sk` to `_ended_sessions` and `_session_completed` (`:1477-1481`) and renders A's result into the current UI.
5. B's deltas are now rejected by `_ended_sessions` (`:1022-1026`), and B's completion is treated as a duplicate (`:1478-1481`). B can produce no response.

The same sequence applies to `_do_error` (`:1808-1812`).

**Root cause:** Only deltas carry turn identity. Completion and error dispatch wrappers accept only `(session_key, text/message)` (`:1439-1447`, `:1777-1779`), so an old completion is indistinguishable from the current turn's completion.  
**Fix:** Capture the turn token in `_on_response_complete`/`_on_error` and pass it through to `_do_response_complete`/`_do_error`; reject callbacks whose token is not the current token before mutating `_ended_sessions` or `_session_completed`. Add completion-after-new-turn and error-after-new-turn tests.

## BUG #2 — same-turn delta question: fixed in the stated FIFO path

**Severity:** none for the specified path  
**Evidence:** `_on_text_delta` captures token `T` and queues `_do_text_delta(..., T)` (`:989-998`). Completion does not modify `_turn_tokens`; therefore, when the queued delta callback runs before the queued completion callback in the normal two-stage FIFO sequence, `T is current_token` and it passes (`:1033-1040`). This removes v3's counter bump failure.

**Boundary:** `_ended_sessions` still drops a delta if completion executes first. That is correct for a callback ordered after completion, but the implementation relies on runtime preserving delta-before-completion callback order. A test must cover both queue orderings and verify the authoritative full-text fallback where completion wins.

## BUG #3 — boolean idempotency question: fixed only within a turn

**Severity:** medium (covered by BUG #1 cross-turn race)  
**Evidence:** For one turn, first completion sees no session in `_session_completed`, adds it, and renders; a second completion sees it and returns (`:1477-1481`). Completion followed by error is also suppressed. Clearing in `send_to_special_agent` correctly permits the next turn.

**Limitation:** Because the set key is only `session_key`, any stale completion from the previous turn can claim the new turn before its real completion. A boolean solves duplicate identity but cannot solve stale callback identity.

## Additional audit notes

- `_turn_tokens` is bounded by active sessions and overwritten per turn; no v3-style counter/set growth was found.
- Early-return behavior remains correct: `_ended_sessions` and `_session_completed` are updated before `_crh is None` (`:1477-1484`), so later deltas are suppressed.
- The token check is bypassed for `delta_token=None` (`:1029-1032`). This preserves legacy two-argument tests but allows untagged production-like callers to evade stale-turn protection. Prefer requiring a token in production paths and test compatibility explicitly.
- No dedicated v4 handler tests were found in `tests/`. Required coverage: FIFO burst deltas, completion idempotency, completion→error, stale delta after new turn, stale completion/error after new turn, `_crh is None`, and missing-token behavior.
