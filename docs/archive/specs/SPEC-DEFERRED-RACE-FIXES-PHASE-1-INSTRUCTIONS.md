# Phase 1 of 1 — Fix 3 deferred race findings in agent_runtime_handler.py

**Spec:** `docs/specs/SPEC-DEFERRED-RACE-FIXES.md`
**Master prompt:** `prompts/steelFramedCodeWriter.md` — invoke it. Read it first.
**Scope:** ONE file: `ui/handlers/agent_runtime_handler.py`. No other files.

## Why

Three race-condition findings remain unfixed:
1. Cross-turn stale deltas (turn A delta survives into turn B)
2. Duplicate completion renders two bubbles
3. Early-return gap (`_crh is None` before flag set)

This fix adds a **turn generation counter** (incremented on the main thread
inside `_do_response_complete`/`_do_error`) and a **completion idempotency set**.

## CRITICAL: Why this generation counter is different from the failed one

The PREVIOUS generation counter incremented in `_on_response_complete` (before
`_do_response_complete` ran), which dropped ALL deltas. This version increments
INSIDE `_do_response_complete` (the main-thread execution point). The increment
happens synchronously on the main thread when completion actually executes.

## Task — SIX edits in `ui/handlers/agent_runtime_handler.py`

Read the file first. Find each site by context. Make ONLY the change shown.

### Edit 1: Add `_turn_generation` + `_completed_turns` to `__init__` (after `_ended_sessions`)

Find `self._ended_sessions: set[str] = set()` and add after it:
```python
        # RACE-FIX v3: per-session turn generation. Incremented at the TOP of
        # _do_response_complete and _do_error (main thread, synchronously).
        # Deltas capture the generation when _on_text_delta runs (main thread).
        # A delta with an old generation (from a previous turn) is dropped.
        self._turn_generation: dict[str, int] = {}
        # RACE-FIX v3: track which (session_key, generation) pairs have already
        # been completed. Prevents duplicate completion from rendering twice.
        self._completed_turns: set[tuple[str, int]] = set()
```

### Edit 2: Capture generation in `_on_text_delta`

Current:
```python
        if self._GLib is not None:
            self._GLib.idle_add(self._do_text_delta, session_key, text)
        else:
            self._do_text_delta(session_key, text)
```

Fixed:
```python
        gen = self._turn_generation.get(session_key, 0)
        if self._GLib is not None:
            self._GLib.idle_add(self._do_text_delta, session_key, text, gen)
        else:
            self._do_text_delta(session_key, text, gen)
```

### Edit 3: Add `delta_gen` param + stale check in `_do_text_delta`

Signature change: `def _do_text_delta(self, session_key: str, text: str) -> None:`
→ `def _do_text_delta(self, session_key: str, text: str, delta_gen: int | None = None) -> None:`

After the existing `_ended_sessions` check block (the `if session_key in
self._ended_sessions: ... return` block), and BEFORE `# Always accumulate text`,
add:
```python
        # RACE-FIX v3: If this delta belongs to a previous turn (generation
        # mismatch), drop it. This handles the cross-turn case where
        # send_to_special_agent cleared _ended_sessions for the new turn
        # but a stale delta from the old turn is still in the idle queue.
        # delta_gen=None means a 2-arg caller (backward-compat tests) —
        # treat as current generation (never stale).
        if delta_gen is not None:
            current_gen = self._turn_generation.get(session_key, 0)
            if delta_gen < current_gen:
                logger.debug(
                    "_do_text_delta: dropping stale delta (gen %d < current %d) for %s",
                    delta_gen, current_gen, session_key,
                )
                return
```

### Edit 4: Increment generation + idempotency at TOP of `_do_response_complete`

Find the current top:
```python
        if self._crh is None:
            return

        # RACE-FIX: Mark session ended IMMEDIATELY — before any rendering work.
        # ...comment block...
        self._ended_sessions.add(session_key)
```

Replace the ENTIRE block from `if self._crib is None:` through `self._ended_sessions.add`
with:
```python
        # RACE-FIX v3: Mark session ended + increment generation BEFORE any
        # rendering work or early returns. This ensures:
        # 1. Stale deltas see the flag (regardless of idle ordering)
        # 2. The generation bump happens on the main thread (deterministic)
        # 3. Even if _crh is None, the flag is set (fixes early-return gap)
        self._ended_sessions.add(session_key)
        gen = self._turn_generation.get(session_key, 0)
        self._turn_generation[session_key] = gen + 1
        # Idempotency: if this (session, gen) was already completed, skip.
        # Prevents duplicate completion from rendering two bubbles.
        if (session_key, gen) in self._completed_turns:
            logger.debug("_do_response_complete: duplicate completion for %s gen %d, skipping", session_key, gen)
            return
        self._completed_turns.add((session_key, gen))

        if self._crh is None:
            return
```

**IMPORTANT:** The `if self._crh is None: return` moves to AFTER the flag/generation/idempotency. This fixes the early-return gap (finding #3).

### Edit 5: Increment generation + idempotency at TOP of `_do_error`

Find the current top:
```python
        # RACE-FIX: Mark session ended IMMEDIATELY (same pattern as _do_response_complete).
        self._ended_sessions.add(session_key)
        logger.debug("[handler] _do_error: sk=%s msg=%s", session_key, message)
```

Replace with:
```python
        # RACE-FIX v3: Mark session ended + increment generation (same as _do_response_complete).
        self._ended_sessions.add(session_key)
        gen = self._turn_generation.get(session_key, 0)
        self._turn_generation[session_key] = gen + 1
        if (session_key, gen) in self._completed_turns:
            logger.debug("_do_error: duplicate completion for %s gen %d, skipping", session_key, gen)
            return
        self._completed_turns.add((session_key, gen))

        logger.debug("[handler] _do_error: sk=%s msg=%s", session_key, message)
```

### Edit 6: Clear completed_turns in `send_to_special_agent`

Find `self._ended_sessions.discard(session_key)` (the one right before
`rt.send_message`) and add after it:
```python
        self._ended_sessions.discard(session_key)
        # RACE-FIX v3: Clear turn tracking for the new turn.
        self._completed_turns.discard((session_key, self._turn_generation.get(session_key, 0)))
```

## Rules

- **One file only:** `ui/handlers/agent_runtime_handler.py`.
- **Do NOT change `_on_response_complete` or `_on_error`** — the generation
  increment happens inside `_do_response_complete` and `_do_error` (the
  main-thread execution point), NOT in the `_on_*` dispatch wrappers.
- **`delta_gen: int | None = None` default is critical** for backward compat
  with 2-arg test callers.
- **The idempotency check uses `(session_key, gen)` tuples** — the gen captured
  BEFORE the increment (the turn being completed), not after.

## Verify (run these, paste full output)

1. Compile:
   ```
   python3 -m py_compile ui/handlers/agent_runtime_handler.py && echo COMPILE_OK
   ```

2. `_turn_generation` references:
   ```
   grep -n "_turn_generation" ui/handlers/agent_runtime_handler.py
   ```
   Expected: init (1), _on_text_delta (1), _do_text_delta (1), _do_response_complete (2), _do_error (2) = 7+ matches

3. `_completed_turns` references:
   ```
   grep -n "_completed_turns" ui/handlers/agent_runtime_handler.py
   ```
   Expected: init (1), _do_response_complete (2), _do_error (2), send_to_special_agent (1) = 6+ matches

4. `_do_text_delta` signature:
   ```
   grep -n "def _do_text_delta" ui/handlers/agent_runtime_handler.py
   ```
   Expected: includes `delta_gen: int | None = None`

5. Idempotency check in _do_response_complete:
   ```
   grep -n "duplicate completion" ui/handlers/agent_runtime_handler.py
   ```
   Expected: 2 matches (one in _do_response_complete, one in _do_error)

6. `_crh is None` check is AFTER flag set in _do_response_complete:
   ```
   sed -n '/def _do_response_complete/,/if self._crh is None/p' ui/handlers/agent_runtime_handler.py | head -20
   ```
   Verify `_ended_sessions.add` appears BEFORE `if self._crh is None`.

7. Run existing tests:
   ```
   python3 -m pytest tests/test_agent_runtime.py -q -k "drawer_lifecycle or ended_sessions or stale or text_delta or response_complete" 2>&1 | tail -5
   ```

## COMPLETENESS checklist

```
COMPLETENESS:
- [x/not done] _turn_generation + _completed_turns added to __init__ — evidence: <grep>
- [x/not done] _on_text_delta captures gen — evidence: <grep>
- [x/not done] _do_text_delta has delta_gen param + stale check — evidence: <grep>
- [x/not done] _do_response_complete increments gen + idempotency at top (before _crh check) — evidence: <grep>
- [x/not done] _do_error increments gen + idempotency at top — evidence: <grep>
- [x/not done] send_to_special_agent clears completed_turns — evidence: <grep>
- [x/not done] py_compile passes — evidence: COMPILE_OK
- [x/not done] existing tests pass — evidence: <pytest summary>
```

Please write per the steelFramedCodeWriter prompt.
