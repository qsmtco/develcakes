# PHASE 1b (Audit Fixes) — Error Context Reachability

**Spec:** `docs/specs/SPEC-SSE-FRAME-SHARDENING.md` (audit follow-up)
**Files to change:** `agent/runtime.py`, `ui/handlers/agent_runtime_handler.py`

---

## BUG #1 — `_crabcakes_context` is unreachable

**Problem:** The runtime's `_on_error` callback is always called with a STRING (`str(exc)`), never the exception object. So `_last_error_exception[session_key]` is always `None`, and `_do_error` can never read `_crabcakes_context`.

**Root cause:** In `agent/runtime.py`, all call sites do `self._dispatch(self._on_error, session_key, str(exc))` or similar — they convert the exception to a string before dispatching.

**Fix:** In `agent/runtime.py`, find the `_call_llm_streaming` exception handler (around line 2621) where `_crabcakes_context` is attached. After attaching context and re-raising, the exception propagates to `_run_loop`, which catches it and calls `self._on_error`. Change that catch site to pass the exception object itself (not `str(exc)`) to `_on_error`.

**Read the actual code first.** Find where `_run_loop` catches the exception from `_call_llm` / `_call_llm_streaming` and calls `self._on_error`. There are multiple `_on_error` call sites in the file. The one that matters is the catch block around the LLM call in `_run_loop`.

**Expected change pattern:**

Current (approximately):
```python
        except Exception as exc:
            logger.error("Error in tool loop for %s", session_key)
            self._dispatch(self._on_error, session_key, str(exc))
```

After:
```python
        except Exception as exc:
            logger.error("Error in tool loop for %s", session_key)
            self._dispatch(self._on_error, session_key, exc)
```

The key change: pass `exc` (the exception object) instead of `str(exc)`. The UI handler's `_on_error` already has an `isinstance(message, BaseException)` check that stores it in `_last_error_exception`.

**IMPORTANT:** Read every `_on_error` call site in `agent/runtime.py`. If any of them pass `str(exc)` or a plain string where they COULD pass the exception object, change them. But only change call sites that are on an exception-handling path (inside an `except` block) — don't change call sites that pass a hardcoded error string like `"conversation not found"`.

---

## BUG #2 — Non-streaming path lacks annotation symmetry

**Problem:** The `except (IndexError, KeyError, TypeError, ValueError)` handler that attaches `_crabcakes_context` only wraps the streaming call (`_call_llm_streaming`). The non-streaming call (`caller(...)` at around line 2629) has no such handler.

**Fix:** Wrap the non-streaming caller call in the same exception handler pattern.

**Read the actual code around lines 2620-2650.** There should be a branching point where the code decides between streaming and non-streaming. The streaming branch already has the annotation. Add the same annotation to the non-streaming branch.

**Expected change pattern:**

Current (approximately):
```python
        if streamer and ...:
            # streaming path
            try:
                return self._call_llm_streaming(...)
            except (IndexError, KeyError, TypeError, ValueError) as e:
                e._crabcakes_context = {...}
                raise
        else:
            # non-streaming path — NO annotation
            return caller(...)
```

After:
```python
        if streamer and ...:
            # streaming path
            try:
                return self._call_llm_streaming(...)
            except (IndexError, KeyError, TypeError, ValueError) as e:
                e._crabcakes_context = {...}
                raise
        else:
            # non-streaming path
            try:
                return caller(...)
            except (IndexError, KeyError, TypeError, ValueError) as e:
                e._crabcakes_context = {
                    "provider": caller_key,
                    "model": model,
                    "exception_type": type(e).__name__,
                }
                raise
```

Use the same variable names (`caller_key`, `model`) that the streaming branch uses. Read the actual code to get the right variable names.

---

## Rules

- Use the `steelFramedCodeWriter` prompt at `prompts/steelFramedCodeWriter.md`.
- **Read `agent/runtime.py` around lines 2600-2660 and all `_on_error` call sites before editing.**
- Do NOT touch test files. Do NOT touch the hardening code itself (Fixes 1-6 from Phase 1).
- Do NOT touch BUG #3 (test_streaming.py segfault) — it's pre-existing and out of scope.

## Verification commands

```bash
cd /path/to/projects/crabcakes

# 1. _on_error call sites — verify at least one passes exc object not str(exc)
grep -n '_on_error.*str(exc)\|_on_error.*exc)' agent/runtime.py

# 2. Non-streaming annotation exists
grep -n '_crabcakes_context' agent/runtime.py
# Expected: at least 2 matches (streaming + non-streaming)

# 3. Existing tests still pass
python3 -m pytest tests/test_agent_runtime.py::TestSSEFrameShapeHardening -v

# 4. Syntax check
python3 -c "import ast; ast.parse(open('agent/runtime.py').read()); print('SYNTAX OK')"
```

## Deliverables

```
COMPLETENESS:
- [x/not done] BUG #1: _on_error passes exception object on error paths — evidence: (command 1)
- [x/not done] BUG #2: non-streaming path has _crabcakes_context annotation — evidence: (command 2)
- [x/not done] Tests still pass — evidence: (command 3)
- [x/not done] Syntax OK — evidence: (command 4)
```
