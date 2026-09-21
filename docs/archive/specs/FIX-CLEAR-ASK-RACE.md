# FIX: /clear + /ask Race Condition (status_code=2013 "chat content is empty")

**Date:** 2026-07-20
**Status:** Draft — for implementation
**Blocking:** SPEC-CONTEXT-MD-SYSTEM-FIX loop (auditor Debugger is dead)
**Severity:** bug
**Pattern:** `race-condition`

## Root Cause

The `/clear @Agent` + `/ask @Agent` pairing rule (from `implementationSupervisor.md` §9.9) creates a race condition:

1. `/ask @Debugger` dispatches first. It spawns a background thread running `_run_loop` (line 944 of `agent/runtime.py`). The thread appends the user message to `conv.messages` and begins iteration 1.
2. `/clear @Debugger` dispatches second, on the main thread. It calls `AgentRuntimeHandler.clear_conversation()`, which sets `conv.messages = []` (line 437 of `ui/handlers/agent_runtime_handler.py`).
3. The background thread, mid-iteration, reaches `messages = conv.to_api_messages()` (line ~1306 of `agent/runtime.py`). `conv.messages` is now empty, so it returns `[{"role":"system", ...}]` — a single-element list with no user turn.
4. `_call_llm` receives `msg_count=1` (system only). MiniMax rejects: `status_code=2013: invalid params, chat content is empty`.

The `clear_conversation` docstring claims "in-place reset avoids races with in-flight tool loops," but reassigning `conv.messages = []` is destructive to a running loop's `to_api_messages()` call. The claim is wrong.

## Fix — Two Layers

### Layer 1: Refuse-clear-while-active (defense in depth, PRIMARY)

`AgentRuntimeHandler.clear_conversation()` MUST refuse to reset a session that has an in-flight tool loop. It returns `False`, and the caller (`cmd_clear`) tells the user to retry.

**`agent/runtime.py`** — add a per-session active-loop tracking set:

```python
# In AgentRuntime.__init__, after self._cancelled (line ~728):
self._cancelled: set[str] = set()
self._cancel_requested: bool = False
self._lock = threading.Lock()
self._running = False

# NEW: sessions with an in-flight _run_loop. Used by is_loop_active()
# and maintained by _run_loop's try/finally. See FIX-CLEAR-ASK-RACE.
self._active_loops: set[str] = set()
```

**`agent/runtime.py`** — in `_run_loop`, wrap the body in try/finally to track active state:

```python
def _run_loop(self, session_key: str, text: str) -> None:
    """Background thread: run the full tool loop for one user message."""
    # FIX-CLEAR-ASK-RACE: mark this session as having an active loop so
    # clear_conversation() can refuse to wipe it mid-turn. Cleared in the
    # finally block below.
    with self._lock:
        self._active_loops.add(session_key)
    try:
        with self._lock:
            if not self._running:
                return
            conv = self._conversations.get(session_key)
            if conv is None:
                self._dispatch(self._on_error, session_key, "No conversation found")
                return
        # ... existing body unchanged ...
    except Exception as e:
        # ... existing except unchanged ...
    finally:
        # FIX-CLEAR-ASK-RACE: always release the active-loop marker, even on
        # exception or early return, so a crashed loop doesn't permanently
        # block /clear for this session.
        with self._lock:
            self._active_loops.discard(session_key)
```

**IMPORTANT — restructure the existing try/except:**

The current `_run_loop` body is wrapped in a single `try:` whose `except Exception` is at line ~1641. The fix RESTRUCTURES this: the new outer try/finally wraps the existing try/except. The active-loop marker is added BEFORE the existing try, and discarded in a NEW finally that runs AFTER the existing except. The existing exception handling logic is UNCHANGED — do not touch the inner try/except, only add the outer try/finally and the marker add/discard.

Structure after the fix:
```python
def _run_loop(self, session_key, text):
    with self._lock:
        self._active_loops.add(session_key)
    try:                              # ← NEW outer try
        with self._lock:              # existing
            ...                       # existing
        try:                          # existing inner try
            ... body ...
        except Exception as e:        # existing inner except
            ... existing handling ...
    finally:                          # ← NEW finally
        with self._lock:
            self._active_loops.discard(session_key)
```

**`agent/runtime.py`** — add a public query method:

```python
def is_loop_active(self, session_key: str) -> bool:
    """Return True if a _run_loop thread is currently active for this session.

    FIX-CLEAR-ASK-RACE: used by AgentRuntimeHandler.clear_conversation() to
    refuse wiping a conversation that an in-flight loop is still reading.
    Thread-safe via _lock. A session marked active stays active until the
    loop's finally block discards it — including through exceptions and
    early returns, so a crashed loop cannot permanently block /clear.
    """
    with self._lock:
        return session_key in self._active_loops
```

### Layer 2: clear_conversation refuses when loop active

**`ui/handlers/agent_runtime_handler.py`** — `clear_conversation()`:

Find the existing block (around line 437):

```python
        conv = rt.get_conversation(session_key)
        if conv is not None:
            try:
                conv.messages = []
```

ADD a guard BEFORE `conv = rt.get_conversation(...)`:

```python
        # FIX-CLEAR-ASK-RACE: refuse to wipe a conversation that an in-flight
        # _run_loop is actively reading. The /clear + /ask pairing rule can
        # fire /clear while the /ask thread is between add_user_message and
        # to_api_messages; wiping conv.messages at that instant produces a
        # system-only payload that MiniMax rejects (status_code=2013). Refuse
        # instead; the user can retry /clear once the loop finishes.
        if rt.is_loop_active(session_key):
            logger.warning(
                "clear_conversation: refusing reset for %s — tool loop is active; retry after it completes",
                session_key,
            )
            return False

        conv = rt.get_conversation(session_key)
        if conv is not None:
            try:
                conv.messages = []
```

### Layer 3: cmd_clear surfaces the refusal (user-facing)

**`ui/handlers/project_handler.py`** — `cmd_clear()` already handles `ok=False`:

The existing code at line ~725 does:
```python
            if ok:
                # UI side effect...
            # (implicit else: ok is False)
```

Find the `if ok:` block. After its end (before the next `return CommandResult(...)` or `else`), add an explicit else that surfaces the refusal reason:

```python
            if ok:
                # ... existing UI side-effect block unchanged ...
            else:
                return CommandResult(
                    handled=True,
                    response_text=(
                        f"Could not clear {agent_name}: a tool loop is currently running. "
                        f"Wait for it to finish, then run /clear again."
                    ),
                )
```

If the existing `cmd_clear` already has an explicit `else` branch, merge this message into it. If it returns a generic "Clear failed" message, replace that text with the loop-active message. Read the actual current code before deciding — do not assume.

## Files Changed

| File | Change |
|------|--------|
| `agent/runtime.py` | +3 init lines, +try/finally wrap on `_run_loop`, +`is_loop_active()` method |
| `ui/handlers/agent_runtime_handler.py` | +6-line guard block in `clear_conversation` |
| `ui/handlers/project_handler.py` | explicit refusal message in `cmd_clear` (else branch) |

## Verification

1. `grep -n "_active_loops" agent/runtime.py` — expect ≥ 4 matches (init, add, discard, is_loop_active)
2. `grep -n "is_loop_active" agent/runtime.py ui/handlers/agent_runtime_handler.py` — expect ≥ 3 matches (def, call in handler, possibly docstring)
3. `grep -n "FIX-CLEAR-ASK-RACE" agent/runtime.py ui/handlers/agent_runtime_handler.py` — expect ≥ 4 matches
4. `python3 -c "from agent.runtime import AgentRuntime; print('OK', hasattr(AgentRuntime, 'is_loop_active'))"` — prints `OK True`
5. `python3 -m pytest tests/test_agent_runtime.py -q` — all existing tests pass (no regression)
6. `python3 -m pytest tests/test_agent_runtime_handler.py -q` — all existing tests pass (if file exists)
7. The existing `test_tool_loop_invokes_tool_chain` and related regression tests still pass
