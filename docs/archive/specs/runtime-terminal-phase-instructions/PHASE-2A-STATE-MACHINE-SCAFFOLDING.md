# Phase 2a — State machine scaffolding in `agent/runtime.py` (no terminal callers yet)

**Spec:** `docs/specs/SPEC-RUNTIME-TERMINAL-PATH-CONSOLIDATION.md` §2.2 Edits A, B, C, E, F, G + H (scaffolding only; the terminal-path routing in Edits D.1–D.7, Q, R is Phase 2b)
**Scope:** `agent/runtime.py` only. Add the state machine infrastructure WITHOUT changing any existing terminal path behavior. After this phase, `_terminate_turn` exists but nothing calls it yet (except the new `RUNNING`/`STREAMING` init in `_run_loop`).

## Goal

Add the per-turn state machine data structures, the `_terminate_turn` chokepoint method, and public accessors. Initialize `RUNNING` state at the top of `_run_loop` and transition to `STREAMING` before the first LLM call. **Do NOT modify any existing `self._dispatch(self._on_*)` + `self._auto_save` + `return` terminal blocks in this phase** — those are Phase 2b.

## Files to change

1. `agent/runtime.py` — 8 edits (described below).

## Required reading first

Read these files IN FULL before writing any code:
- `agent/runtime.py` (the whole file — especially `__init__` ~lines 320-430, `_dispatch` ~line 420, `_run_loop` ~lines 879-1440, `is_loop_active` ~line 1442, `_check_and_stop_on_limit` ~line 1868, `cancel` ~line 661, `send_message` ~line 632)
- `agent/callbacks.py` (Phase 1 output — the 9 Protocols you'll import for the `__init__` type hints)
- `docs/specs/SPEC-RUNTIME-TERMINAL-PATH-CONSOLIDATION.md` §2.2 Edits A, B, C, E, F, G, H

## Edits

### Edit A — Add `TurnStatus` enum and `TurnResult` dataclass

**Location:** Top of `agent/runtime.py`, after the `StreamingCallKwargs` TypedDict (around line 76) and before `__all__`.

Add:
```python
from enum import Enum

class TurnStatus(Enum):
    """Per-turn state. Transitions are owned by _terminate_turn.

    RUNNING: Turn started, no LLM call yet.
    STREAMING: At least one LLM call returned; text/tool-calls may be in flight.
    COMPLETED: Terminal success.
    FAILED: Terminal failure — error dispatched, partial state persisted.
    CANCELLED: Terminal user-initiated cancellation.

    Invariant: a turn transitions RUNNING → STREAMING → exactly one of
    {COMPLETED, FAILED, CANCELLED}. STREAMING is non-terminal.
    """
    RUNNING = "running"
    STREAMING = "streaming"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclasses.dataclass
class TurnResult:
    """Everything a terminal callback needs in one struct. Built by callers
    of _terminate_turn; dispatched to the handler by _terminate_turn.

    Fields:
        status: Terminal status (COMPLETED/FAILED/CANCELLED).
        session_key: The session whose turn ended.
        turn_token: Identity object set at send_message/_run_loop time.
        text: Final assistant text (COMPLETED). Empty for FAILED/CANCELLED.
        error: Error that caused termination. None for COMPLETED. str or BaseException.
        metadata: Free-form dict. Keys vary by status:
            COMPLETED: {"fallback_used": bool, "stream_error": dict|None}
            FAILED: {"reason": str, "iteration": int, ...}
            CANCELLED: {"reason": "user"|"shutdown", "iteration": int}
    """
    status: TurnStatus
    session_key: str
    turn_token: object
    text: str = ""
    error: str | BaseException | None = None
    metadata: dict = dataclasses.field(default_factory=dict)
```

**Important:** Check if `from enum import Enum` is already imported (`grep -n "from enum\|^import enum" agent/runtime.py`). If not, add it. `dataclasses` is already imported (the file uses `import dataclasses`).

Update `__all__` to add `"TurnStatus"` and `"TurnResult"`:
```python
__all__ = [
    "AgentRuntime",
    "SSEEvent",
    "StreamingCallKwargs",
    "_PROVIDER_CALLERS",
    "_PROVIDER_STREAMERS",
    "TurnStatus",
    "TurnResult",
]
```
(Keep `_PROVIDER_STREAMERS` for now — it's removed in Phase 4, not this phase.)

### Edit B — Add state machine fields to `__init__`

**Location:** In `AgentRuntime.__init__`, after the `_active_loops` declaration (around line 369). Add:

```python
        # Turn state machine (SPEC-RUNTIME-TERMINAL-PATH-CONSOLIDATION §2.2).
        # _turn_tokens: session_key → active turn_token. Rotated by _run_loop
        #   at start so stale terminal results are rejected.
        # _turn_state: (session_key, turn_token) → current TurnStatus.
        # _turn_results: (session_key, turn_token) → most recent terminal TurnResult.
        # _state_lock: dedicated lock for ALL state-machine reads/writes.
        #   The GIL does NOT make the compound read→decide→write atomic (BUG #3).
        self._state_lock = threading.Lock()
        self._turn_tokens: dict[str, object] = {}
        self._turn_state: dict[tuple[str, object], TurnStatus] = {}
        self._turn_results: dict[tuple[str, object], TurnResult] = {}
```

### Edit C — Add `_terminate_turn` method

**Location:** After `_dispatch_enforcement_status` (around line 457) and before the lifecycle methods. Insert the method from spec §2.2 Edit C verbatim. Key points:
- Signature: `def _terminate_turn(self, result: TurnResult) -> TurnResult | None:`
- Rejects non-terminal statuses (RUNNING/STREAMING) → returns None, logs error.
- Acquires `self._state_lock` for the stale-token check + duplicate-terminal check + state write.
- Stale-token check: if `_turn_tokens[sk]` exists and `is not result.turn_token`, reject (return None, log).
- Duplicate-terminal check: if `_turn_state[(sk,tk)]` is already terminal, reject (return None, log).
- Records state + result under lock.
- Dispatches OUTSIDE the lock: `on_response_complete` for COMPLETED, `on_error` for FAILED/CANCELLED.
- Persists via `_auto_save` for COMPLETED/FAILED; for CANCELLED only if `metadata.get("persist", False)`.
- Cleans up `_tool_history` via `_cleanup_tool_history(sk)` for FAILED/CANCELLED.
- Returns `result` if accepted, `None` if rejected.

Use the spec §2.2 Edit C text as the authoritative source. The method is ~80 lines including docstring.

### Edit E — Add public accessors

**Location:** After `is_loop_active` (around line 1442). Add two methods:

```python
    def get_last_turn_result(self, session_key: str) -> TurnResult | None:
        """Return the most recent terminal TurnResult for the session's active token."""
        with self._state_lock:
            tk = self._turn_tokens.get(session_key)
            if tk is None:
                return None
            return self._turn_results.get((session_key, tk))

    def get_turn_state(self, session_key: str) -> TurnStatus | None:
        """Return the current TurnStatus for the session's active token."""
        with self._state_lock:
            tk = self._turn_tokens.get(session_key)
            if tk is None:
                return None
            return self._turn_state.get((session_key, tk))
```

### Edit F — Initialize RUNNING at the top of `_run_loop`

**Location:** At the very top of `_run_loop`, BEFORE the `self._active_loops.add(session_key)` line (around line 883). Actually: insert RIGHT AFTER the active_loops add, before the `try:` block. The state must be registered before the missing-conversation check.

The current top of `_run_loop`:
```python
    def _run_loop(self, session_key: str, text: str, turn_token: object = None) -> None:
        """Background thread: run the full tool loop for one user message."""
        with self._lock:
            self._active_loops.add(session_key)
        try:
```

Change to:
```python
    def _run_loop(self, session_key: str, text: str, turn_token: object = None) -> None:
        """Background thread: run the full tool loop for one user message."""
        with self._lock:
            self._active_loops.add(session_key)
        # Turn state machine: register the active token and init RUNNING.
        # Done BEFORE the conv/prompt checks so all terminal paths have
        # a well-defined starting state (BUG #2).
        with self._state_lock:
            self._turn_tokens[session_key] = turn_token
            self._turn_state[(session_key, turn_token)] = TurnStatus.RUNNING
        try:
```

**DO NOT change the `self._dispatch(self._on_error, "No conversation found", ...)` or prompt-build-failure dispatches in this phase.** Those stay as-is (Phase 2b routes them through `_terminate_turn`). For Phase 2a, the only behavioral change is: RUNNING state is now set; the early-exit paths still dispatch via `_dispatch` directly but the state machine is initialized.

### Edit G — Transition to STREAMING before first LLM call

**Location:** Immediately before the first `response = self._call_llm(...)` call (around line 1040). Insert:

```python
                    # Turn state machine: transition to STREAMING (non-terminal).
                    with self._state_lock:
                        if self._turn_state.get((session_key, turn_token)) == TurnStatus.RUNNING:
                            self._turn_state[(session_key, turn_token)] = TurnStatus.STREAMING
```

This goes right before `response = self._call_llm(session_key, messages_for_call, tools, turn_token=turn_token)`.

### Edit H — Import callback Protocols and update `__init__` type hints

**Location:** Imports section (after the `agent.audit` / `agent.persistence` imports around line 38) and `__init__` signature (around line 320-336).

Add import:
```python
from agent.callbacks import (
    OnTextDelta, OnToolCallStart, OnToolCallResult, OnToolCallApprovalNeeded,
    OnResponseComplete, OnTokenUsage, OnTokenBreakdown, OnError, OnEnforcementStatus,
)
```

Update `__init__` parameter types from `Callable | None` to the specific Protocol:
```python
        on_text_delta: OnTextDelta | None = None,
        on_tool_call_start: OnToolCallStart | None = None,
        on_tool_call_result: OnToolCallResult | None = None,
        on_tool_call_approval_needed: OnToolCallApprovalNeeded | None = None,
        on_response_complete: OnResponseComplete | None = None,
        on_token_usage: OnTokenUsage | None = None,
        on_token_breakdown: OnTokenBreakdown | None = None,
        on_error: OnError | None = None,
        on_enforcement_status: OnEnforcementStatus | None = None,
```

The `self._on_*` attribute assignments (`self._on_text_delta = on_text_delta` etc.) stay unchanged.

### Edit (docstring) — Class docstring update

**Location:** Replace the class docstring at the top of `AgentRuntime` (around lines 290-307) with the threading-model docstring from spec §2.2 Edit H. This documents `self._lock` vs `self._state_lock` and what is NOT synchronized.

## Verification commands (run all, paste output)

```bash
# 1. Compiles
python3 -m py_compile agent/runtime.py && echo COMPILE_OK

# 2. TurnStatus + TurnResult importable
python3 -c "from agent.runtime import TurnStatus, TurnResult; print({s.value for s in TurnStatus}); import dataclasses; print([f.name for f in dataclasses.fields(TurnResult)])"
# Expected: {'running', 'streaming', 'completed', 'failed', 'cancelled'}
#           ['status', 'session_key', 'turn_token', 'text', 'error', 'metadata']

# 3. State machine fields exist
python3 -c "import agent.runtime as r; print([a for a in ('_state_lock','_turn_tokens','_turn_state','_turn_results') if hasattr(r.AgentRuntime, '__init__')])"
# (Better: check via instance) — see step 4

# 4. _terminate_turn exists
grep -n "def _terminate_turn" agent/runtime.py

# 5. _state_lock used in _terminate_turn
grep -n "with self._state_lock:" agent/runtime.py

# 6. RUNNING initialized at top of _run_loop
grep -n "TurnStatus.RUNNING" agent/runtime.py

# 7. STREAMING transition before _call_llm
grep -n "TurnStatus.STREAMING" agent/runtime.py

# 8. Accessors exist
grep -n "def get_last_turn_result\|def get_turn_state" agent/runtime.py

# 9. Protocol imports added
grep -n "from agent.callbacks import" agent/runtime.py

# 10. __init__ uses Protocol types
grep -n "on_text_delta: OnTextDelta\|on_error: OnError" agent/runtime.py

# 11. CRITICAL: no existing terminal path was changed
#     The count of _dispatch(self._on_error) and _dispatch(self._on_response_complete)
#     must be UNCHANGED from baseline.
grep -c "self._dispatch(self._on_error" agent/runtime.py
grep -c "self._dispatch(self._on_response_complete" agent/runtime.py
# (Baseline: _on_error appears in _terminate_turn body [NEW] + existing sites.
#  In this phase, _terminate_turn is NOT yet called by _run_loop, so existing
#  _dispatch sites are untouched. The only NEW _dispatch(self._on_error) is
#  INSIDE _terminate_turn itself.)

# 12. Full test suite — existing failures unchanged, no NEW failures
XDG_CONFIG_HOME=/tmp/cctest_home/.config timeout 120 python3 -m pytest tests/test_agent_runtime.py -q --no-header --timeout=15 2>&1 | tail -5
# Expected: 19 failed, 153 passed (same baseline count — no regressions from scaffolding)
```

## CRITICAL constraints

- **DO NOT modify any existing `self._dispatch(self._on_*)` call site in `_run_loop`, `cancel()`, or anywhere else.** Phase 2a is scaffolding only. The existing terminal behavior must be byte-for-byte identical.
- **DO NOT call `_terminate_turn` from anywhere except its own definition.** It's defined but uncalled. Phase 2b wires the callers.
- **DO NOT remove or change `_PROVIDER_CALLERS`, `_PROVIDER_STREAMERS`, `_call_openai`, etc.** Those are Phase 4.
- `_terminate_turn` references `_cleanup_tool_history` — that method already exists (line ~1861). Do not modify it.
- `_terminate_turn` references `_auto_save` — that method already exists. Do not modify it.
- `_terminate_turn` references `_dispatch` — that method already exists. Do not modify it.

## COMPLETENESS checklist (mandatory)

```
COMPLETENESS:
- [x/not done] Edit A: TurnStatus enum + TurnResult dataclass added — evidence: import output
- [x/not done] Edit B: _state_lock + _turn_tokens + _turn_state + _turn_results in __init__ — evidence: grep
- [x/not done] Edit C: _terminate_turn method added (returns TurnResult|None, uses _state_lock) — evidence: grep + line count
- [x/not done] Edit E: get_last_turn_result + get_turn_state accessors — evidence: grep
- [x/not done] Edit F: RUNNING initialized at top of _run_loop — evidence: grep
- [x/not done] Edit G: STREAMING transition before first _call_llm — evidence: grep
- [x/not done] Edit H: callback Protocol imports + __init__ type hints — evidence: grep
- [x/not done] Class docstring updated (threading model) — evidence: head -50
- [x/not done] py_compile OK — evidence: step 1
- [x/not done] No existing terminal path modified — evidence: step 11 grep counts match expectation
- [x/not done] Test suite: 19 failed / 153 passed (no new failures) — evidence: step 12 output
```

Report all verification outputs. Flag any spec drift (line numbers moved) with the corrected location.
