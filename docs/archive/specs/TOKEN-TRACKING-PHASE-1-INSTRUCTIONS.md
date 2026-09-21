# PHASE 1 of 2 — Token Tracking: agent_runtime_handler.py + `_session_usage` dict

**Spec:** `docs/specs/SPEC-token-tracking-fix.md` (parent spec)
**Status:** NOT STARTED — verified gap in codebase
**Goal:** Add an in-memory `_session_usage` dict to `AgentRuntimeHandler` that caches per-session token usage from `_on_token_usage` callbacks, and expose it via a `get_session_usage()` getter.

## Files to Change

1. `ui/handlers/agent_runtime_handler.py` — add `_session_usage` dict, update `_on_token_usage`, add `get_session_usage()`

## Instructions

### Read First (ALL of these, completely)

- `ui/handlers/agent_runtime_handler.py` — the full file, focus on:
  - `__init__` (line ~34-100) — where instance variables are declared
  - `_on_token_usage` (line ~1174) — the callback that currently only logs
  - The `AgentRuntime(...)` constructor call (line ~540) — where `on_token_usage=self._on_token_usage` is wired
- `models/conversation.py` — see `total_tokens` and `total_cost` fields on the `Conversation` dataclass
- `agent/runtime.py` lines around 1798-1801 — see how `_on_token_usage` is dispatched from the runtime

### Edit 1: Add `_session_usage` dict to `__init__`

In the `__init__` method of `AgentRuntimeHandler`, after the existing instance variable declarations (after `self._pending_exec_commands` at approximately line 100), add:

```python
        # Per-session token usage cache: session_key → (total_tokens, total_cost)
        # Populated by _on_token_usage, read by get_session_usage().
        self._session_usage: dict[str, tuple[int, float]] = {}
```

### Edit 2: Update `_on_token_usage` to store in addition to logging

The current method at approximately line 1174:

```python
    def _on_token_usage(self, session_key: str, total_tokens: int, cost: float) -> None:
        """AgentRuntime token usage callback. Logged for now."""
        logger.info(
            "Special agent token usage for %s: %d tokens, $%.4f",
            session_key,
            total_tokens,
            cost,
        )
```

Change to:

```python
    def _on_token_usage(self, session_key: str, total_tokens: int, cost: float) -> None:
        """AgentRuntime token usage callback. Store and log."""
        self._session_usage[session_key] = (total_tokens, cost)
        logger.info(
            "Special agent token usage for %s: %d tokens, $%.4f",
            session_key,
            total_tokens,
            cost,
        )
```

### Edit 3: Add a public getter method

Add a method that returns a copy of the usage cache. Place it right after `_on_token_usage`:

```python
    def get_session_usage(self) -> dict[str, tuple[int, float]]:
        """Return the in-memory session usage cache.

        Keyed by session_key. Values are (total_tokens, total_cost).
        Used by /cost command as fallback for agents without conversation files.
        Returns a defensive copy.
        """
        return dict(self._session_usage)
```

## Rules

- Use the `steelFramedCodeWriter` prompt at `prompts/steelFramedCodeWriter.md`
- READ ALL FILES BEFORE STARTING — read every file mentioned above in full before writing any code
- Run: `python3 -m pytest tests/test_agent_runtime.py -q --tb=short -x` and paste the output
- Run: `grep -n "_session_usage" ui/handlers/agent_runtime_handler.py` and paste output (should show 3+ matches: init, store, getter)
- Report: files changed with line numbers, test results, any issues
- At the end, include a COMPLETENESS checklist:
  COMPLETENESS:
  - [x/not done] Edit 1: _session_usage dict added to __init__ — evidence
  - [x/not done] Edit 2: _on_token_usage stores to dict — evidence
  - [x/not done] Edit 3: get_session_usage getter method — evidence
  - [x/not done] Tests pass — paste output
