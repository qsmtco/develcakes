# Phase 2b — Route terminal paths through `_terminate_turn` (HIGH RISK)

**Spec:** `docs/specs/SPEC-RUNTIME-TERMINAL-PATH-CONSOLIDATION.md` §2.2 Edits D.1–D.7, Q, R
**Scope:** `agent/runtime.py` only. Rewire 7+ existing terminal dispatch blocks to call `_terminate_turn`. Refactor `_check_and_stop_on_limit` to a pure predicate. Fix `cancel()` token.

## Goal

Replace the 5+ ad-hoc `self._dispatch(self._on_*) + self._auto_save + return` triplets scattered across `_run_loop` with a single call to `self._terminate_turn(TurnResult(...))`. After this phase, `_terminate_turn` is the ONLY terminal dispatch path.

## Required reading first

Read these IN FULL:
- `agent/runtime.py` — especially `_run_loop` (lines ~1190-1620), `_check_and_stop_on_limit` (lines ~1875-1895), `cancel` (lines ~661-680). The line numbers have drifted from the spec; USE FUNCTION/IDENTIFIER ANCHORS, not line numbers.
- The current `_terminate_turn` method (lines ~629-780) — understand what it does so your replacements are correct.
- `docs/specs/SPEC-RUNTIME-TERMINAL-PATH-CONSOLIDATION.md` §2.2 Edits D.1-D.7, Q, R.

## Edits

### Edit D.1 — Cancellation paths in `_run_loop` (2 sites)

**Site 1:** The `if self._cancel_requested:` block near the top of the while loop. Currently:
```python
                    if self._cancel_requested:
                        self._cancel_requested = False
                        self._dispatch(self._on_error, session_key, "Cancelled", _turn_token=turn_token)
                        return
```
Replace with:
```python
                    if self._cancel_requested:
                        self._cancel_requested = False
                        self._terminate_turn(TurnResult(
                            status=TurnStatus.CANCELLED,
                            session_key=session_key,
                            turn_token=turn_token,
                            error="Cancelled",
                            metadata={"reason": "shutdown", "iteration": iteration},
                        ))
                        return
```

**Site 2:** The `if session_key in self._cancelled:` block right after site 1. Currently:
```python
                    with self._lock:
                        if session_key in self._cancelled:
                            self._cancelled.discard(session_key)
                            self._dispatch(self._on_error, session_key, "Cancelled", _turn_token=turn_token)
                            return
```
Replace with:
```python
                    with self._lock:
                        if session_key in self._cancelled:
                            self._cancelled.discard(session_key)
                            self._terminate_turn(TurnResult(
                                status=TurnStatus.CANCELLED,
                                session_key=session_key,
                                turn_token=turn_token,
                                error="Cancelled",
                                metadata={"reason": "user", "iteration": iteration},
                            ))
                            return
```

### Edit D.2 — Empty/missing content error path

**Anchor:** The block that does `conv.add_assistant_message("[LLM returned no content — provider error or malformed response]", [])` followed by a `try: self._dispatch(self._on_error, session_key, error_text, ...)` + `self._auto_save(...)` + `return`.

Currently:
```python
                            conv.add_assistant_message(
                                "[LLM returned no content — provider error or malformed response]",
                                [],
                            )
                            try:
                                self._dispatch(self._on_error, session_key, error_text, _turn_token=turn_token)
                            except Exception as _e:
                                logger.error("[tool-loop] sk=%s _on_error handler raised %s: %s — continuing with save+return",
                                             session_key, type(_e).__name__, _e)
                            self._auto_save(session_key, conv)
                            return
```
Replace with (the `try/except` around dispatch is subsumed by `_terminate_turn` which dispatches via `_dispatch` which already catches exceptions):
```python
                            conv.add_assistant_message(
                                "[LLM returned no content — provider error or malformed response]",
                                [],
                            )
                            self._terminate_turn(TurnResult(
                                status=TurnStatus.FAILED,
                                session_key=session_key,
                                turn_token=turn_token,
                                error=error_text,
                                metadata={"reason": "empty_content", "iteration": iteration},
                            ))
                            return
```

### Edit D.3 — Mid-stream error with non-empty content (BUG #5 — BEHAVIOR CHANGE)

**Anchor:** The block that checks `stream_err = response.get("_stream_error")` and dispatches `on_error`. Currently this block dispatches `on_error` but does NOT return — it falls through to the text-success path below (dispatching BOTH error AND response_complete for the same turn). This is the bug.

Currently:
```python
                        stream_err = response.get("_stream_error")
                        if stream_err:
                            # ... build error_text ...
                            try:
                                self._dispatch(self._on_error, session_key, error_text, _turn_token=turn_token)
                            except Exception as _e:
                                logger.error("[tool-loop] sk=%s _on_error handler raised %s: %s — continuing",
                                             session_key, type(_e).__name__, _e)
```
Replace with (ADD the explicit `return` after `_terminate_turn`):
```python
                        stream_err = response.get("_stream_error")
                        if stream_err:
                            # ... build error_text (keep all the err_code/err_msg/metadata logic) ...
                            logger.warning("[tool-loop] sk=%s stream error with non-empty content: %s",
                                           session_key, error_text)
                            self._terminate_turn(TurnResult(
                                status=TurnStatus.FAILED,
                                session_key=session_key,
                                turn_token=turn_token,
                                error=error_text,
                                metadata={"reason": "stream_error_with_content", "iteration": iteration},
                            ))
                            return
```
**CRITICAL:** The `return` after `_terminate_turn` is MANDATORY (BUG #5). Without it, the code falls through to the text-success dispatch below. Keep ALL the error_text-building logic (err_code, err_msg, metadata provider_name) — only replace the dispatch+try/except with the `_terminate_turn` + `return`.

### Edit D.4 — Text-only response success path

**Anchor:** The block after the stream-error check that does `conv.add_assistant_message(text_content, [])` + `_dispatch(self._on_response_complete, ...)` + `_check_and_stop_on_limit(...)` + `_auto_save(...)` + `return`.

Currently:
```python
                        logger.debug("[tool-loop] sk=%s text-only response, dispatching on_response_complete len=%d",
                                     session_key, len(text_content or ""))
                        conv.add_assistant_message(text_content, [])
                        self._dispatch(self._on_response_complete, session_key, text_content, _turn_token=turn_token)
                        self._check_and_stop_on_limit(session_key, conv)
                        self._auto_save(session_key, conv)
                        return
```
Replace with:
```python
                        logger.debug("[tool-loop] sk=%s text-only response, dispatching on_response_complete len=%d",
                                     session_key, len(text_content or ""))
                        conv.add_assistant_message(text_content, [])
                        # _check_and_stop_on_limit is now a pure predicate (Edit Q).
                        # If a limit is hit, terminate FAILED; otherwise terminate COMPLETED.
                        limit_result = self._check_and_stop_on_limit(session_key, conv)
                        if limit_result is not None:
                            stopped_reason, reason_msg = limit_result
                            conv.add_assistant_message(f"[stopped: {reason_msg}]", [])
                            self._terminate_turn(TurnResult(
                                status=TurnStatus.FAILED,
                                session_key=session_key,
                                turn_token=turn_token,
                                error=reason_msg,
                                metadata={"reason": stopped_reason, "iteration": iteration},
                            ))
                            return
                        self._terminate_turn(TurnResult(
                            status=TurnStatus.COMPLETED,
                            session_key=session_key,
                            turn_token=turn_token,
                            text=text_content,
                            metadata={
                                "fallback_used": getattr(conv, "_fallback_attempted", False),
                                "stream_error": response.get("_stream_error"),
                            },
                        ))
                        return
```

### Edit D.5 — Max iterations reached

**Anchor:** After the while loop, the block `conv.add_assistant_message("[max tool iterations reached]", [])` + dispatch + save.

Currently:
```python
                # Max iterations reached
                conv.add_assistant_message("[max tool iterations reached]", [])
                self._dispatch(self._on_error, session_key, "Max tool iterations reached", _turn_token=turn_token)
                self._auto_save(session_key, conv)
```
Replace with (no explicit `return` needed — control falls out of the while loop; the original had no return here either):
```python
                # Max iterations reached
                conv.add_assistant_message("[max tool iterations reached]", [])
                self._terminate_turn(TurnResult(
                    status=TurnStatus.FAILED,
                    session_key=session_key,
                    turn_token=turn_token,
                    error="Max tool iterations reached",
                    metadata={"reason": "max_iterations", "iterations": max_iter},
                ))
```

### Edit D.6 — Top-level exception handler

**Anchor:** The `except Exception as e:` block at the end of the `try:` in `_run_loop`.

Currently:
```python
            except Exception as e:
                logger.exception("Error in tool loop for %s", session_key)
                try:
                    self._auto_save(session_key, conv)
                except Exception:
                    logger.exception("Failed to auto_save after tool-loop error for %s", session_key)
                self._dispatch(self._on_error, session_key, e, _turn_token=turn_token)
```
Replace with:
```python
            except Exception as e:
                logger.exception("Error in tool loop for %s", session_key)
                self._terminate_turn(TurnResult(
                    status=TurnStatus.FAILED,
                    session_key=session_key,
                    turn_token=turn_token,
                    error=e,
                    metadata={"reason": "exception", "exception_type": type(e).__name__},
                ))
```

### Edit D.7 — Post-tool-execution limit check (line ~1416 in original)

**Anchor:** The block `if self._check_and_stop_on_limit(session_key, conv): return` after tool execution.

Currently:
```python
                    # Check cost/step limits after tool execution
                    if self._check_and_stop_on_limit(session_key, conv):
                        return
```
Replace with:
```python
                    # Check cost/step limits after tool execution
                    limit_result = self._check_and_stop_on_limit(session_key, conv)
                    if limit_result is not None:
                        stopped_reason, reason_msg = limit_result
                        conv.add_assistant_message(f"[stopped: {reason_msg}]", [])
                        self._terminate_turn(TurnResult(
                            status=TurnStatus.FAILED,
                            session_key=session_key,
                            turn_token=turn_token,
                            error=reason_msg,
                            metadata={"reason": stopped_reason, "iteration": iteration},
                        ))
                        return
```

### Edit Q — Refactor `_check_and_stop_on_limit` to a pure predicate (BUG #6, #11)

**Anchor:** The method `def _check_and_stop_on_limit(self, session_key: str, conv: Any) -> bool:`

This method currently: (1) checks cost/step limits, (2) dispatches `on_error` with an undefined `turn_token` variable (BUG #6 — NameError), (3) calls `_auto_save` (BUG #11 — bypasses state machine), (4) adds an assistant message placeholder. ALL of (2), (3), (4) must be removed — the method becomes a pure predicate.

Replace the ENTIRE method body with:
```python
    def _check_and_stop_on_limit(
        self, session_key: str, conv: Any,
    ) -> tuple[str, str] | None:
        """Check cost and step limits. Pure predicate — no side effects.

        Returns:
            None if the turn should continue.
            (stopped_reason, error_message) if a limit is exceeded, where
            stopped_reason is "cost_limit" or "step_limit".

        Audit fixes (BUG #6, #11): the previous version dispatched on_error
        (with an undefined turn_token — NameError), called _auto_save, and
        added an assistant message. All side effects removed; the caller
        (_run_loop) builds the TurnResult and routes through _terminate_turn.
        """
        if self._config.cost_limit is not None and conv.total_cost > self._config.cost_limit:
            reason = (
                f"Cost limit exceeded: ${conv.total_cost:.4f} "
                f"> ${self._config.cost_limit:.4f}"
            )
            return ("cost_limit", reason)
        if self._config.step_limit is not None and conv.step_count > self._config.step_limit:
            reason = (
                f"Step limit exceeded: {conv.step_count} > {self._config.step_limit}"
            )
            return ("step_limit", reason)
        return None
```

### Edit D.8 — Fix `cancel()` to use active session token (BUG #13)

**Anchor:** The `cancel` method. Currently it dispatches `on_error` with `self._turn_token` (the runtime's global, possibly-stale token).

Currently:
```python
    def cancel(self, session_key: str) -> None:
        """Cancel an in-progress conversation."""
        with self._lock:
            # Mark as cancelled so _run_loop's check will catch it
            self._cancelled.add(session_key)
            # Signal the running thread to break out of the loop immediately
            self._cancel_requested = True
            for sk in list(self._pending_approvals):
                if sk.startswith(session_key):
                    ev = self._pending_approvals[sk]["event"]
                    self._pending_approvals[sk]["result"] = None
                    ev.set()
            self._dispatch(self._on_error, session_key, "Cancelled by user", _turn_token=self._turn_token)
            logger.info("Cancelled session %s", session_key)
        # §E: Clean up stuck-detection history when conversation ends
        self._cleanup_tool_history(session_key)
```

Replace with:
```python
    def cancel(self, session_key: str) -> None:
        """Cancel an in-progress conversation.

        Signals the running thread and dispatches a user-facing cancellation
        message. Uses the active turn token from _turn_tokens[session_key]
        (not the runtime's global _turn_token which may be stale). The
        background thread's _run_loop will call _terminate_turn(CANCELLED)
        from its cancellation check; the dispatch here is the UX path so
        the user sees the message immediately. _terminate_turn's dedup
        ensures only one terminal transition is recorded.
        """
        with self._lock:
            self._cancelled.add(session_key)
            self._cancel_requested = True
            for sk in list(self._pending_approvals):
                if sk.startswith(session_key):
                    ev = self._pending_approvals[sk]["event"]
                    self._pending_approvals[sk]["result"] = None
                    ev.set()
        # Clean up stuck-detection history. _terminate_turn will also call
        # _cleanup_tool_history (idempotent), but doing it here ensures
        # cleanup even if the background thread is wedged.
        self._cleanup_tool_history(session_key)
        # Dispatch the user-facing cancellation message using the ACTIVE
        # token for this session (BUG #13: not the runtime's global token).
        with self._state_lock:
            active_tk = self._turn_tokens.get(session_key, self._turn_token)
        self._dispatch(
            self._on_error, session_key, "Cancelled by user",
            _turn_token=active_tk,
        )
        logger.info("Cancelled session %s", session_key)
```

Note: the `self._dispatch(...)` was moved OUTSIDE the `with self._lock:` block to avoid holding `_lock` during dispatch. The `_cleanup_tool_history` and `_state_lock` reads are also outside `_lock`.

## Verification commands (run all, paste output)

```bash
# 1. Compiles
python3 -m py_compile agent/runtime.py && echo COMPILE_OK

# 2. _terminate_turn is now CALLED from _run_loop (at least 7 sites)
grep -c "self._terminate_turn(" agent/runtime.py
# Expected: >= 8 (7 terminal paths in _run_loop + the _turn_tokens defensive write inside _terminate_turn does NOT count; it's "self._turn_tokens" not "self._terminate_turn")
# Actually: count should be >= 7 (D.1×2 + D.2 + D.3 + D.4 + D.5 + D.6 + D.7 = 8 call sites in _run_loop)

# 3. NO remaining ad-hoc _dispatch(self._on_response_complete) outside _terminate_turn
grep -n "self._dispatch(self._on_response_complete" agent/runtime.py
# Expected: exactly 1 match, and it must be INSIDE _terminate_turn (line ~639)

# 4. NO remaining ad-hoc _dispatch(self._on_error) in _run_loop's terminal paths
# (cancel() keeps its own dispatch — that's intentional per Edit D.8)
grep -n "self._dispatch(self._on_error" agent/runtime.py
# Expected: the _terminate_turn internal dispatch + cancel()'s dispatch + any non-terminal dispatches. The KEY check: no _dispatch(self._on_error) in _run_loop's try block except inside _terminate_turn.

# 5. _check_and_stop_on_limit is a pure predicate (no dispatch, no save)
grep -A 20 "def _check_and_stop_on_limit" agent/runtime.py | grep -E "_dispatch|_auto_save|add_assistant_message"
# Expected: 0 matches (the method only returns tuples now)

# 6. cancel() uses _turn_tokens, not _turn_token
grep -A 5 "def cancel" agent/runtime.py | grep "_turn_token=self._turn_token"
# Expected: 0 matches (the stale global-token pattern is gone)

# 7. The NameError bug is gone (no bare turn_token in _check_and_stop_on_limit)
sed -n '/def _check_and_stop_on_limit/,/return None/p' agent/runtime.py | grep "turn_token"
# Expected: 0 matches (turn_token is not referenced in the pure predicate)

# 8. Full test suite
XDG_CONFIG_HOME=/tmp/cctest_home/.config timeout 120 python3 -m pytest tests/test_agent_runtime.py -q --no-header --timeout=15 2>&1 | tail -5
# Expected: STILL 19 failed / 153 passed (the 15 turn_token-kwarg failures persist — Phase 3 fixes the test mocks; the 4 pre-existing GTK/drawer failures persist). NO new failures.
```

## CRITICAL constraints

- **The D.3 return is mandatory.** Without it, on_error AND on_response_complete both fire for mid-stream errors. This is a behavior change from baseline (where only on_error fired but the turn continued). Verify with the grep.
- **_check_and_stop_on_limit must have ZERO side effects** after the refactor. No dispatch, no save, no add_assistant_message. The caller builds the TurnResult.
- **cancel()'s dispatch stays** — it's the UX path. _terminate_turn's dedup handles the background thread's later CANCELLED call.
- **Do NOT change the missing-conversation or prompt-build-failure early-exit dispatches** — those are NOT in this phase's scope (they stay as direct _dispatch calls). Actually, wait — the spec §2.2 Edit F (Phase 2a) initialized RUNNING before them. In THIS phase, route those 2 paths through _terminate_turn too:

### Edit D-extra.1 — Missing conversation early-exit

**Anchor:** `if conv is None: self._dispatch(self._on_error, session_key, "No conversation found", ...); return`

Currently:
```python
                conv = self._conversations.get(session_key)
                if conv is None:
                    self._dispatch(self._on_error, session_key, "No conversation found", _turn_token=turn_token)
                    return
```
Replace with:
```python
                conv = self._conversations.get(session_key)
                if conv is None:
                    self._terminate_turn(TurnResult(
                        status=TurnStatus.FAILED,
                        session_key=session_key,
                        turn_token=turn_token,
                        error="No conversation found",
                        metadata={"reason": "no_conversation"},
                    ))
                    return
```

### Edit D-extra.2 — Prompt-build-failure early-exit

**Anchor:** `except Exception as e: self._dispatch(self._on_error, session_key, e, ...); return` (the `_ensure_system_prompt` try/except)

Currently:
```python
            try:
                self._ensure_system_prompt(session_key)
            except Exception as e:
                self._dispatch(self._on_error, session_key, e, _turn_token=turn_token)
                return
```
Replace with:
```python
            try:
                self._ensure_system_prompt(session_key)
            except Exception as e:
                self._terminate_turn(TurnResult(
                    status=TurnStatus.FAILED,
                    session_key=session_key,
                    turn_token=turn_token,
                    error=e,
                    metadata={"reason": "prompt_build_failed", "exception_type": type(e).__name__},
                ))
                return
```

## COMPLETENESS checklist (mandatory)

```
COMPLETENESS:
- [x/not done] D.1: cancellation paths (×2) routed through _terminate_turn — evidence: grep
- [x/not done] D.2: empty-content error routed — evidence: grep
- [x/not done] D.3: mid-stream error routed + explicit return added — evidence: grep (return within 5 lines of _terminate_turn)
- [x/not done] D.4: text-success routed (with limit check) — evidence: grep
- [x/not done] D.5: max-iterations routed — evidence: grep
- [x/not done] D.6: top-level exception routed — evidence: grep
- [x/not done] D.7: post-tool-execution limit check updated — evidence: grep
- [x/not done] D-extra.1: missing-conversation routed — evidence: grep
- [x/not done] D-extra.2: prompt-build-failure routed — evidence: grep
- [x/not done] Edit Q: _check_and_stop_on_limit is pure predicate (0 side effects) — evidence: grep step 5
- [x/not done] Edit D.8: cancel() uses _turn_tokens[session_key] — evidence: grep step 6
- [x/not done] Only 1 _dispatch(self._on_response_complete) remains (inside _terminate_turn) — evidence: grep step 3
- [x/not done] py_compile OK — evidence: step 1
- [x/not done] Test suite: 19 failed / 153 passed (no new failures) — evidence: step 8
```

Report all verification outputs. Flag any spec drift with corrected locations.
