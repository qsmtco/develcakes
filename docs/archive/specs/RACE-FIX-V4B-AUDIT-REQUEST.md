# Audit Request: Race Fix v4b (tokens on ALL callbacks)

## What changed from v4

v4 had tokens on deltas only. Debugger found BUG #1 (CRITICAL): stale
completion/error from turn A could terminate turn B because completion/error
callbacks had no token.

v4b adds token capture + check to ALL three callback types:
- `_on_text_delta` → `_do_text_delta` (delta_token) — already in v4
- `_on_response_complete` → `_do_response_complete` (complete_token) — NEW
- `_on_error` → `_do_error` (error_token) — NEW

All three `_do_*` methods check `token is not current_token` at the top,
before any state mutation.

## The complete token flow

1. `send_to_special_agent`: `self._turn_tokens[sk] = object()` — new unique token per turn
2. `_on_text_delta`: captures `token = self._turn_tokens.get(sk)` → passes to `_do_text_delta`
3. `_on_response_complete`: captures `token = self._turn_tokens.get(sk)` → passes to `_do_response_complete`
4. `_on_error`: captures `token = self._turn_tokens.get(sk)` → passes to `_do_error`
5. Each `_do_*` checks: `if token is not None and token is not current_token: return`

## Why this fixes all findings

- **v3 BUG #1/#3 (same-turn deltas dropped):** Token doesn't change at completion. Same-turn deltas always match. ✓
- **v3 BUG #2 (idempotency broken):** `_session_completed` is a boolean set, not a counter. ✓
- **v4 BUG #1 (stale completion terminates new turn):** Completion now carries a token. A stale turn-A completion has token T_A ≠ T_B (current) → rejected. ✓
- **Early-return gap:** Flag set before `_crh` check. ✓
- **Unbounded growth:** `_session_completed` is cleared per turn; `_turn_tokens` is overwritten per turn. ✓

## Key question

In the normal FIFO burst:
1. `_on_text_delta` captures T → queues `_do_text_delta(T)`
2. `_on_response_complete` captures T → queues `_do_response_complete(T)`
3. `_do_text_delta(T)` runs → T is current → PASSES ✓
4. `_do_response_complete(T)` runs → T is current → PASSES → renders ✓

Cross-turn stale completion:
1. Turn A: token T_A assigned
2. Turn A completion queued with T_A but not yet executed
3. Turn B: `send_to_special_agent` assigns T_B, clears flags
4. Turn A `_do_response_complete(T_A)` runs → T_A ≠ T_B → REJECTED ✓

Is this correct? Any remaining race?

Write findings to `docs/specs/RACE-FIX-V4B-FINDINGS.md`.
