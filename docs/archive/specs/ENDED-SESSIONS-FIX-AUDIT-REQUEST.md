# Audit Request: _ended_sessions Race Fix (generation counter reverted)

## Context

The generation-counter approach was WRONG — it dropped ALL deltas (not just
stale ones) because `_on_response_complete` incremented the generation BEFORE
the queued `_do_text_delta` callbacks ran. The supervisor reverted it entirely
and replaced it with an `_ended_sessions` guard.

## The new fix (3 changes in agent_runtime_handler.py)

### Change 1: _do_text_delta checks _ended_sessions at the top

After the `if not text: return` guard, before text accumulation:
```python
if session_key in self._ended_sessions:
    logger.debug("_do_text_delta: dropping delta for ended session %s", session_key)
    return
```

### Change 2: _ended_sessions.discard moved OUT of _do_text_delta

Previously, `_do_text_delta` cleared `_ended_sessions` inside the streaming-start
block (line ~1021). This was the original race bug: a stale delta arriving after
completion would clear the flag and start a new phantom streaming bubble.

Now `_ended_sessions` is cleared ONLY in `send_to_special_agent` (line 856),
right before `rt.send_message()` — the actual start of a new turn.

### Change 3: generation counter fully reverted

- `_delta_generation` dict removed from __init__
- `_on_text_delta` no longer captures/passes generation
- `_do_text_delta` signature reverted to `(self, session_key, text)`
- `_on_response_complete` no longer increments generation
- `_on_error` no longer increments generation

## The race this should fix

1. Background thread dispatches deltas + completion via GLib.idle_add
2. Main thread runs `_do_response_complete` → renders final bubble → sets `_ended_sessions`
3. Stale `_do_text_delta` runs → sees `_ended_sessions` → returns immediately (no new bubble)

## Questions for the auditor

1. Does `_ended_sessions` get set BEFORE or AFTER the stale delta could run?
   Trace the idle-queue ordering. `_do_response_complete` sets the flag at the
   END (line ~1543). If a stale `_do_text_delta` is queued AFTER
   `_do_response_complete` in the idle queue, does the flag get set in time?

2. The `_ended_sessions` flag is set at the END of `_do_response_complete`
   (line ~1543). But `_do_response_complete` is ALSO dispatched via idle_add
   (from `_on_response_complete`). So the ordering is:
   - idle queue: [_do_text_delta (stale), _do_response_complete]
   - _do_text_delta runs FIRST → _ended_sessions not yet set → NOT dropped → starts streaming!
   - _do_response_complete runs SECOND → sets flag (too late)
   
   IS THIS THE SAME BUG IN A DIFFERENT FORM?

3. Is there a gap between `end_streaming` (which removes the streaming bubble)
   and `_ended_sessions.add` (which is at the very end of _do_response_complete)?
   If a stale delta runs in that gap, does it start a new bubble?

4. Could `_ended_sessions` block the FIRST delta of a legitimate new turn?
   `send_to_special_agent` clears it (line 856), but is there a race where
   the first delta arrives before `send_to_special_agent`'s idle callback runs?

## Files to read

- `ui/handlers/agent_runtime_handler.py` — `_do_text_delta` (984+),
  `send_to_special_agent` (743+), `_do_response_complete` (1426+),
  `_on_response_complete` (1415+), `_on_text_delta` (970+)

Write findings to `docs/specs/ENDED-SESSIONS-FIX-FINDINGS.md`.
