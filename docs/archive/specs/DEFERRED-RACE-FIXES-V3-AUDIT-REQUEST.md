# Audit Request: Deferred Race Fixes v3 (generation counter + idempotency)

## Context

Previous attempts:
- v1 (generation counter in `_on_response_complete`): FAILED — incremented
  before `_do_response_complete` ran, dropping ALL deltas
- v2 (`_ended_sessions` at end of `_do_response_complete`): FAILED — flag set
  too late, stale delta ran before flag existed
- v2b (`_ended_sessions` at TOP of `_do_response_complete`): WORKED for primary
  bug but had 3 deferred findings (cross-turn stale deltas, duplicate completion,
  early-return gap)

v3 fixes all 3 deferred findings.

## The 3 fixes (6 edits in agent_runtime_handler.py)

### Fix 1: Turn generation counter (cross-turn stale deltas)
- `_turn_generation: dict[str, int]` tracks per-session turn number
- `_on_text_delta` captures gen at dispatch time
- `_do_text_delta` checks `delta_gen < current_gen` → drops stale deltas
- Generation is incremented INSIDE `_do_response_complete` and `_do_error`
  (main-thread execution point), NOT in `_on_response_complete` (the dispatch wrapper)

### Fix 2: Completion idempotency (duplicate completion)
- `_completed_turns: set[tuple[str, int]]` tracks (session, gen) pairs
- `_do_response_complete` checks `(session_key, gen) in _completed_turns` → skips
- `_do_error` does the same
- `send_to_special_agent` clears the entry for the new turn

### Fix 3: Early-return gap
- `_ended_sessions.add` + generation increment moved BEFORE `if self._crh is None`
- Even if `_crh` is None, the flag is set

## Key ordering invariants

1. In `_do_response_complete`: `_ended_sessions.add` → gen increment → idempotency
   check → `_crh` check → rendering
2. In `_do_error`: same pattern
3. In `_do_text_delta`: `_crh` check → empty-text check → `_ended_sessions` check
   → generation check → accumulate text
4. Generation increments in `_do_*` (main thread), NOT in `_on_*` (dispatch wrappers)

## Questions for the auditor

1. **Does the generation counter correctly distinguish same-turn deltas from
   cross-turn deltas?** Trace: turn A deltas capture gen=0, completion bumps to
   gen=1. Turn B starts (send_to_special_agent doesn't change gen). Turn B
   deltas capture gen=1 (current). A stale turn-A delta has gen=0 < 1 → dropped.
   Is this correct?

2. **Does the idempotency check prevent duplicate completion?** First call:
   `(sk, 0)` not in set → adds it → renders. Second call: `(sk, 0)` in set →
   skips. But wait — the first call INCREMENTED gen to 1. The second call
   captures `gen = self._turn_generation.get(sk, 0)` which is now 1, not 0.
   So the second call checks `(sk, 1)` which is NOT in the set → it DOESN'T
   skip. IS THE IDEMPOTENCY CHECK CORRECT?

3. **Does the early-return gap fix work?** If `_crh is None`, the flag and gen
   are set, then return. Next stale delta sees the flag → dropped. Correct?

4. **Could the generation counter drop legitimate same-turn deltas?** A delta
   captures gen=0. `_do_response_complete` runs (bumps to 1). Then the delta
   runs with gen=0 < 1 → DROPPED. But this delta was from the SAME turn!
   Is this the same bug as v1?

## File to read
`ui/handlers/agent_runtime_handler.py` — all 6 edit sites

Write findings to `docs/specs/DEFERRED-RACE-FIXES-V3-FINDINGS.md`.
