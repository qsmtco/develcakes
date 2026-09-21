# PHASE 5 — Fix Stuck-Message Double-Pop (W12)

## Objective
Remove the dead second pop of `_pending_stuck_messages` in `_call_llm_streaming`.

## Files to Read First
- `/path/to/projects/crabcakes/agent/runtime.py` (lines 1915–1960, 2027–2060)
- `/path/to/projects/crabcakes/docs/specs/SPEC-RUNTIME-HARDENING-AUDIT.md` (W12)

## Step 1 — Locate Both Pops

**Read lines 1915–1960** to find the first pop in `_call_llm`.

Search for `_pending_stuck_messages.pop` in the file:
```bash
grep -n "_pending_stuck_messages.pop" /path/to/projects/crabcakes/agent/runtime.py
```

You should find two occurrences:
1. In `_call_llm` (~line 1930): **KEEP THIS**
2. In `_call_llm_streaming` (~line 2054): **REMOVE THIS**

## Step 2 — Read the Streaming Function

**Read lines 2027–2060** to see the full context of the second pop.

The second pop is inside a `finally` block in `_call_llm_streaming`. It is dead
code because `_call_llm` already pops `_pending_stuck_messages` before it ever
calls `_call_llm_streaming`.

## Step 3 — Remove the Second Pop

In `_call_llm_streaming`, find the `finally` block that contains:
```python
_pending_stuck_messages.pop(streaming_session_key, None)
```

**Remove that entire line** from the finally block.

If the `finally` block becomes empty (only `pass`), remove the `finally: pass` block entirely.

## Step 4 — Verify

```bash
cd /path/to/projects/crabcakes
python3 -c "from agent.runtime import AgentRuntime; print('import ok')"
python3 -m py_compile agent/runtime.py && echo "syntax ok"
grep -n "_pending_stuck_messages.pop" agent/runtime.py
```

Expected output: exactly ONE occurrence (in `_call_llm`).

## What NOT to Change
- Do NOT remove the pop from `_call_llm`
- Do NOT change any other code
