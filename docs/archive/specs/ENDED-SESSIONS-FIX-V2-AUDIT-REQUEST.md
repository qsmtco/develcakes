# Audit Request: _ended_sessions Race Fix v2 (flag moved to TOP)

## Context

The previous version had `_ended_sessions.add` at the END of `_do_response_complete`
and `_do_error`. Debugger found BUG #1 (CRITICAL): a stale `_do_text_delta` queued
BEFORE `_do_response_complete` would run first, pass the guard (flag not set yet),
and start a phantom streaming bubble.

The fix: moved `_ended_sessions.add` to the VERY TOP of both `_do_response_complete`
and `_do_error`, before any rendering work.

## The current fix (4 changes in agent_runtime_handler.py)

### Change 1: _ended_sessions.add at TOP of _do_response_complete (line 1454)

```python
def _do_response_complete(self, session_key: str, text: str) -> None:
    if self._crh is None:
        return
    # RACE-FIX: Mark session ended IMMEDIATELY — before any rendering work.
    self._ended_sessions.add(session_key)
    # ... rest of method (rendering, crabcards, lifecycle) ...
```

### Change 2: _ended_sessions.add at TOP of _do_error (line 1778)

```python
def _do_error(self, session_key: str, message: str) -> None:
    # RACE-FIX: Mark session ended IMMEDIATELY (same pattern as _do_response_complete).
    self._ended_sessions.add(session_key)
    # ... rest of method ...
```

### Change 3: _do_text_delta checks _ended_sessions (line 1006)

```python
def _do_text_delta(self, session_key: str, text: str) -> None:
    if self._crh is None:
        return
    if not text:
        return
    if session_key in self._ended_sessions:
        logger.debug("_do_text_delta: dropping delta for ended session %s", session_key)
        return
    # ... accumulate text, start streaming if needed ...
```

### Change 4: _ended_sessions.discard only in send_to_special_agent (line 856)

```python
def send_to_special_agent(self, session_key: str, text: str) -> None:
    # ... setup ...
    self._ended_sessions.discard(session_key)  # NEW turn starts
    rt.send_message(session_key, text)
```

The old `_ended_sessions.discard()` inside `_do_text_delta` (which cleared the flag
and enabled the race) was removed.

## The idle-queue ordering analysis

With the flag now at the TOP of `_do_response_complete`, the two possible orderings:

**Ordering A (stale delta AFTER completion):**
```
idle: [_do_response_complete, _do_text_delta(stale)]
1. _do_response_complete runs → sets flag at line 1454 FIRST → renders bubble
2. _do_text_delta runs → sees flag → DROPPED ✓
```

**Ordering B (stale delta BEFORE completion):**
```
idle: [_do_text_delta(stale), _do_response_complete]
1. _do_text_delta runs → flag NOT set yet → proceeds → accumulates text → starts/updates streaming
2. _do_response_complete runs → sets flag → renders final bubble via end_streaming → flag now set
3. No more stale deltas → user sees the FINAL bubble (correct!) ✓
```

**Ordering B was the original bug scenario.** But with the flag at the top of
completion, ordering B is now SAFE: the stale delta runs, starts streaming (which
is fine — it's showing partial text), then completion runs and REPLACES the
streaming bubble with the final rendered bubble. The flag prevents any FURTHER
stale deltas from starting a NEW bubble after completion.

## Questions for the auditor

1. In Ordering B, does `_do_response_complete` correctly REPLACE the streaming
   bubble started by the stale delta? Or does it create a DUPLICATE bubble?
   (Trace `was_streaming` + `end_streaming` + the non-streaming fallback.)

2. Can the `_ended_sessions` flag set at the top of `_do_response_complete`
   interfere with `_do_tool_call_start`? (Tool calls check `_ended_sessions`
   at line 1077. If completion sets the flag, can a legitimate tool call from
   the SAME turn be suppressed?)

3. Is there a path where `_do_response_complete` or `_do_error` returns early
   (before setting the flag) leaving the session in a "not ended" state?
   (Check: the `if self._crh is None: return` guard is BEFORE the flag.)

4. Does BUG #2 from the previous audit (new turn reopening stale-callback window)
   still apply? With `_ended_sessions.discard` in `send_to_special_agent`, can
   a queued delta from turn A survive into turn B?

## Files to read

- `ui/handlers/agent_runtime_handler.py` — `_do_text_delta` (984+),
  `send_to_special_agent` (743+), `_do_response_complete` (1424+),
  `_do_error` (1775+), `_do_tool_call_start` (1070+)

Write findings to `docs/specs/ENDED-SESSIONS-FIX-V2-FINDINGS.md`.
