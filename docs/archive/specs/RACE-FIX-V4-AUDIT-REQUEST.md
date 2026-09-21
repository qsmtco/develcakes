# Audit Request: Race Fix v4 (turn token + session_completed)

## What changed from v3

v3 used a generation COUNTER (incremented at completion) which dropped
same-turn deltas (BUG #1/#3) and had a broken idempotency check (BUG #2).

v4 replaces it with:
- **`_session_completed: set[str]`** — a simple boolean. First completion adds
  the session; duplicate completion sees it and returns. No counter, no key
  mismatch.
- **`_turn_tokens: dict[str, object]`** — each turn gets a unique `object()`
  sentinel assigned in `send_to_special_agent`. Deltas capture the token.
  If the token doesn't match the current turn's token, the delta is stale.
  The token does NOT change at completion time — same-turn deltas always match.

## The 4 mechanisms

1. **`_ended_sessions`** (unchanged from v2b): set at top of `_do_response_complete`/`_do_error`. Drops stale deltas that run after completion.

2. **`_session_completed`** (NEW): set at top of `_do_response_complete`/`_do_error`, after `_ended_sessions`. If already in set → duplicate → skip. Cleared in `send_to_special_agent`.

3. **`_turn_tokens`** (NEW): unique `object()` per turn, assigned in `send_to_special_agent`. Deltas capture it in `_on_text_delta`. `_do_text_delta` checks `delta_token is not current_token` → stale → drop. Token never changes at completion, so same-turn deltas always pass.

4. **Early-return gap** (unchanged from v2b): flag set before `_crh is None` check.

## Why v4 fixes v3's bugs

- **BUG #1/#3 (same-turn deltas dropped):** FIXED. The token doesn't change at
  completion. Same-turn deltas capture token T, completion doesn't change T,
  so deltas always match. Only cross-turn deltas (old token ≠ new token) are dropped.

- **BUG #2 (idempotency broken):** FIXED. `_session_completed` is a simple
  boolean set. First call: not in set → add → render. Second call: in set →
  skip. No counter to read a different value from.

- **BUG #4 (unbounded growth):** FIXED. `_session_completed` is a set of
  strings, cleared on each new turn. `_turn_tokens` is a dict, overwritten on
  each new turn. No unbounded growth.

## Key question for the auditor

**Critical:** In the normal two-stage GLib FIFO dispatch:
1. `_on_text_delta` runs (main thread) → captures token T → queues `_do_text_delta(token=T)`
2. `_on_response_complete` runs → queues `_do_response_complete`
3. `_do_text_delta(token=T)` runs → current token is still T → PASSES ✓
4. `_do_response_complete` runs → sets `_ended_sessions` + `_session_completed` → renders

Is step 3 correct? The token T is still the current turn's token (completion
doesn't change it). So same-turn deltas are NOT dropped. Only a delta from a
PREVIOUS turn (token T_old ≠ T_current) would be dropped.

## File
`ui/handlers/agent_runtime_handler.py`

Write findings to `docs/specs/RACE-FIX-V4-FINDINGS.md`.
