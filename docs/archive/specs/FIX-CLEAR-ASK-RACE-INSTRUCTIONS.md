# Fix Instructions — /clear + /ask Race Condition

**Spec:** `docs/specs/FIX-CLEAR-ASK-RACE.md` (read it in FULL first)
**Severity:** bug — blocking the auditor (Debugger), so blocking the whole loop
**Pattern:** `race-condition`

Use the `steelFramedCodeWriter` prompt at `prompts/steelFramedCodeWriter.md`. Read `docs/specs/FIX-CLEAR-ASK-RACE.md` in full. Read each file you touch in full before editing.

## Files (3)

1. `agent/runtime.py` — add `_active_loops` set to `__init__`, wrap `_run_loop` in try/finally, add `is_loop_active()`
2. `ui/handlers/agent_runtime_handler.py` — add guard block in `clear_conversation()`
3. `ui/handlers/project_handler.py` — surface refusal in `cmd_clear()` else branch

---

## Edit 1 — `agent/runtime.py` `__init__`: add `_active_loops`

Find this exact block in `AgentRuntime.__init__` (around line 727):

```python
        self._cancelled: set[str] = set()  # cancelled session keys
        self._cancel_requested: bool = False  # immediate cancel signal for running thread
        self._lock = threading.Lock()
        self._running = False
```

Insert AFTER `self._running = False`:

```python
        self._running = False

        # FIX-CLEAR-ASK-RACE: sessions with an in-flight _run_loop. Used by
        # is_loop_active() and maintained by _run_loop's try/finally.
        self._active_loops: set[str] = set()
```

---

## Edit 2 — `agent/runtime.py` `_run_loop`: wrap in try/finally

The current `_run_loop` structure is:

```python
def _run_loop(self, session_key: str, text: str) -> None:
    """Background thread: run the full tool loop for one user message."""
    with self._lock:
        if not self._running:
            return
        conv = self._conversations.get(session_key)
        if conv is None:
            self._dispatch(self._on_error, session_key, "No conversation found")
            return

    # ... body ...
    try:
        # ... main body ...
    except Exception as e:
        logger.exception("Error in tool loop for %s", session_key)
        try:
            self._auto_save(session_key, conv)
        except Exception:
            logger.exception("Failed to auto_save after tool-loop error for %s", session_key)
        self._dispatch(self._on_error, session_key, e)
```

**You must add a NEW outer try/finally** that wraps the entire body. The active-loop marker is added at the very top (before the `with self._lock` check) and discarded in a NEW finally at the very bottom.

Make these TWO precise changes:

### 2a — Top of function: add marker + open outer try

Find the FIRST line of the function body:

```python
    def _run_loop(self, session_key: str, text: str) -> None:
        """Background thread: run the full tool loop for one user message."""
        with self._lock:
            if not self._running:
                return
```

Replace with:

```python
    def _run_loop(self, session_key: str, text: str) -> None:
        """Background thread: run the full tool loop for one user message."""
        # FIX-CLEAR-ASK-RACE: mark this session as having an active loop so
        # clear_conversation() can refuse to wipe it mid-turn. Cleared in the
        # finally block at the end of this function.
        with self._lock:
            self._active_loops.add(session_key)
        try:
            with self._lock:
                if not self._running:
                    return
```

**CRITICAL:** This change increases the indentation of EVERY line in the function body by 4 spaces. The body between the old `with self._lock:` and the end of the function must all be re-indented one level. Do this carefully — a mis-indented `return` inside the `with self._lock:` block changes semantics.

The structure inside the new outer `try:` becomes:
- `with self._lock:` (the running/conversation check) — 3 levels deep (function body + outer try + with)
- All subsequent body code — 2 levels deep (function body + outer try)

### 2b — Bottom of function: add finally

Find the existing `except Exception as e:` block at the end of `_run_loop`:

```python
        except Exception as e:
            logger.exception("Error in tool loop for %s", session_key)
            try:
                self._auto_save(session_key, conv)
            except Exception:
                logger.exception("Failed to auto_save after tool-loop error for %s", session_key)
            self._dispatch(self._on_error, session_key, e)
```

After this except block (at the function's end), add a NEW finally. The except block stays at its (new) indentation. The new finally is at the SAME indentation as the new outer `try:`:

```python
            except Exception as e:
                logger.exception("Error in tool loop for %s", session_key)
                try:
                    self._auto_save(session_key, conv)
                except Exception:
                    logger.exception("Failed to auto_save after tool-loop error for %s", session_key)
                self._dispatch(self._on_error, session_key, e)
        finally:
            # FIX-CLEAR-ASK-RACE: always release the active-loop marker, even
            # on exception or early return, so a crashed loop doesn't block
            # /clear for this session permanently.
            with self._lock:
                self._active_loops.discard(session_key)
```

**The existing inner try/except is UNCHANGED in logic** — only its indentation increases by 4 spaces (it's now inside the new outer try). The `except Exception as e:` and its body move from 8-space-indent to 12-space-indent. Do not touch the contents of the except block.

**Sanity check after the edit:** the function should parse. If Python complains about indentation, you got the re-indent wrong. Read the full function back and verify every block lines up.

---

## Edit 3 — `agent/runtime.py`: add `is_loop_active()` method

Add this new method. Place it IMMEDIATELY AFTER `_run_loop` ends (after the new `finally:` block), as a new method at the same indentation as `_run_loop`.

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

---

## Edit 4 — `ui/handlers/agent_runtime_handler.py` `clear_conversation()`: add guard

Find this block in `clear_conversation()` (around line 435):

```python
        conv = rt.get_conversation(session_key)
        if conv is not None:
            try:
                conv.messages = []
```

Insert BEFORE the `conv = rt.get_conversation(...)` line:

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

---

## Edit 5 — `ui/handlers/project_handler.py` `cmd_clear()`: surface refusal

Read the existing `cmd_clear()` method (around line 676). Find the `if ok:` block. It currently does UI side effects when `ok` is True.

**If there is NO explicit `else` branch** after the `if ok:` block, add one. **If there IS already an else**, replace its body with the refusal message.

The message must be:

```python
            else:
                return CommandResult(
                    handled=True,
                    response_text=(
                        f"Could not clear {agent_name}: a tool loop is currently running. "
                        f"Wait for it to finish, then run /clear again."
                    ),
                )
```

**READ THE ACTUAL CURRENT CODE** before deciding whether to add or merge the else branch. Report what you found.

---

## Verification (paste full output)

1. `grep -n "_active_loops" agent/runtime.py`
2. `grep -n "is_loop_active" agent/runtime.py ui/handlers/agent_runtime_handler.py`
3. `grep -n "FIX-CLEAR-ASK-RACE" agent/runtime.py ui/handlers/agent_runtime_handler.py ui/handlers/project_handler.py`
4. `python3 -c "from agent.runtime import AgentRuntime; print('OK', hasattr(AgentRuntime, 'is_loop_active'))"`
5. `python3 -m pytest tests/test_agent_runtime.py -q 2>&1 | tail -15`
6. `python3 -m pytest tests/test_project_handler.py -q 2>&1 | tail -15`
7. `python3 -c "import ast; ast.parse(open('agent/runtime.py').read()); print('runtime.py parses OK')"`
8. `python3 -c "import ast; ast.parse(open('ui/handlers/agent_runtime_handler.py').read()); print('handler parses OK')"`

---

## COMPLETENESS checklist (mandatory)

```
COMPLETENESS:
- [x/not done] Edit 1: Added _active_loops to __init__ — evidence: <grep>
- [x/not done] Edit 2a: Added marker + opened outer try at top of _run_loop — evidence: <grep or ast parse>
- [x/not done] Edit 2b: Added finally at bottom of _run_loop — evidence: <grep or ast parse>
- [x/not done] Edit 3: Added is_loop_active() method — evidence: <grep>
- [x/not done] Edit 4: Added guard in clear_conversation — evidence: <grep>
- [x/not done] Edit 5: Surfaced refusal in cmd_clear (added/merged else) — evidence: <grep or diff>
- [x/not done] runtime.py parses (ast.parse OK) — evidence: <command output>
- [x/not done] handler parses — evidence: <command output>
- [x/not done] test_agent_runtime.py passes — evidence: <pytest tail>
- [x/not done] test_project_handler.py passes — evidence: <pytest tail>
```
