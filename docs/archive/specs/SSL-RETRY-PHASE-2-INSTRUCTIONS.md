# PHASE 2 of 3 — Layer 2: `_stream_with_ssl_retry` + wire into `_call_llm_streaming`

**Spec:** `docs/specs/SPEC-SSL-RETRY-FIX.md` (read this first)
**Depends on:** Phase 1 complete

## Files to change

**ONLY:** `agent/runtime.py`

## What to do

### Edit 1: Add `_friendly_error_message()` function

After `_is_retryable_ssl_error()`, add a new function that:
- Accepts `exc: Exception`
- Builds a chain list: `[exc]`, plus `exc.reason` if URLError, plus `exc.__cause__`
- For each candidate in chain:
  - If `isinstance(cand, ssl.SSLError)`:
    - If "EOF" or "shutdown" in `str(exc)`: return "Connection to the AI provider was lost mid-response. Please try sending your message again."
    - Otherwise: return `f"Secure connection error: {raw}. Please try again."`
  - If `isinstance(cand, (ConnectionResetError, BrokenPipeError))`: return "Connection to the AI provider was reset. Please try sending your message again."
- If no SSL/network error in chain: return `str(exc)` (raw passthrough)

### Edit 2: Add `_stream_with_ssl_retry()` generator

After `_urlopen_with_ssl_retry()`, add a new generator function:
- Signature: `def _stream_with_ssl_retry(streamer, *, max_retries=_MAX_SSL_RETRIES, **kwargs)`
- Loop `range(max_retries + 1)`:
  - Track `streamed_text = False` at the top of each attempt
  - `for ev in streamer(**kwargs)`: if `ev.type == "text_delta"`, set `streamed_text = True`, then `yield ev`
  - On success (generator exhausted), `return`
  - `except (ssl.SSLError, ConnectionResetError, BrokenPipeError)`:
    - If `streamed_text` or max attempts or not retryable: `raise`
    - Otherwise: log warning, sleep with backoff, continue loop
  - `except urllib.error.URLError`:
    - Same logic: if `streamed_text` or max attempts or not retryable: `raise`
    - Otherwise: log warning, sleep with backoff, continue loop

### Edit 3: Wire into `_call_llm_streaming`

Find the existing streaming loop in `_call_llm_streaming` that looks like:
```python
for ev in streamer(base_url, api_key, model, messages, tools, timeout, x_title=x_title):
```

Replace it with:
```python
for ev in _stream_with_ssl_retry(
    streamer,
    base_url=base_url,
    api_key=api_key,
    model=model,
    messages=messages,
    tools=tools,
    timeout=timeout,
    x_title=x_title,
):
```

**IMPORTANT:** The streamer is called with keyword args in the wrapper. The original call uses positional args — make sure all kwargs names match the streamer's parameters.

### Edit 4: Wire `_friendly_error_message` into error handler

In `_run_loop`, find the error handler:
```python
except Exception as e:
    logger.exception("Error in tool loop for %s", session_key)
    self._dispatch(self._on_error, session_key, str(e))
```

Replace `str(e)` with `_friendly_error_message(e)`:
```python
    msg = _friendly_error_message(e)
    self._dispatch(self._on_error, session_key, msg)
```

### Edit 5: Append to `__all__`

Add `"_stream_with_ssl_retry"` and `"_friendly_error_message"` to `__all__`.

## Rules

- Use the `steelFramedCodeWriter` prompt at `prompts/steelFramedCodeWriter.md`
- READ ALL FILES BEFORE STARTING — read the current state of `agent/runtime.py` (it was modified in Phase 1)
- Run: `python3 -c "import ast; ast.parse(open('agent/runtime.py').read()); print('Syntax OK')"`
- Run: `python3 -m pytest tests/test_agent_runtime.py -q --tb=short --deselect "tests/test_agent_runtime.py::TestApproval::test_exec_with_approval_allow" --deselect "tests/test_agent_runtime.py::TestApproval::test_exec_with_approval_deny" 2>&1 | tail -5`
- Paste ALL command output
- Report: files changed with line numbers, test results, any issues

## COMPLETENESS checklist required

Please write Phase 2 when ready.
