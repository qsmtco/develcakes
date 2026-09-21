# TimeoutError Retry + Friendly Message — Instructions

**Files:** `agent/llm/streaming.py` + `tests/test_llm_streaming.py`

**FIRST ACTION: Load the steelFramedCodeWriter prompt fresh.** Read `/path/to/projects/crabcakes/prompts/steelFramedCodeWriter.md` IN FULL. Activate it. Begin with Discovery Phase block.

Read `agent/llm/streaming.py` in full before editing.

---

## Context

The Supervisor found that `TimeoutError` (Python 3.10+ alias for `socket.timeout`) is NOT caught by the SSL retry layer. When a provider's SSE stream stalls (no chunk within 120s), the socket raises `TimeoutError`, which propagates through `stream_with_ssl_retry` and `urlopen_with_ssl_retry` unhandled, killing the agent's tool loop. The user sees a raw "The read operation timed out" message with no retry attempt.

The Supervisor has ALREADY applied the retry-layer fix (adding `TimeoutError` to the exception tuples). **Verify it's in place**, then add the friendly error message + tests.

---

## Edit 1 — Verify the retry-layer fix is present

Run:
```bash
grep -n "TimeoutError" agent/llm/streaming.py
```

You should see `TimeoutError` in:
- `RETRYABLE_OSERROR_TYPES` tuple (line ~142)
- `urlopen_with_ssl_retry` except clause (line ~294)
- `stream_with_ssl_retry` except clause (line ~383)

If any of these are missing, add `TimeoutError` to the respective exception tuple. If all 3 are present, skip to Edit 2.

---

## Edit 2 — Add `TimeoutError` to `friendly_error_message`

In `friendly_error_message()` (line ~223), the exception-chain walk currently handles `ssl.SSLError`, `ConnectionResetError`, and `BrokenPipeError`. Add `TimeoutError` handling.

Find the loop that checks `isinstance(cand, ...)`:

```python
    for cand in chain:
        if isinstance(cand, ssl.SSLError):
            text = str(cand)
            lowered = text.lower()
            if "eof" in lowered or "shutdown" in lowered:
                return ("Connection to the AI provider was lost mid-response. "
                        "Please try sending your message again.")
            return f"Secure connection error: {text}. Please try again."
        if isinstance(cand, (ConnectionResetError, BrokenPipeError)):
            return ("Connection to the AI provider was reset. "
                    "Please try sending your message again.")
    return str(exc)
```

Add a `TimeoutError` check BEFORE the `return str(exc)` fallback:

```python
        if isinstance(cand, TimeoutError):
            return ("The AI provider took too long to respond (connection timed out "
                    "after retries). The provider may be slow or overloaded. "
                    "Please try sending your message again.")
    return str(exc)
```

The message tells the user: (a) what happened (timeout), (b) why (provider slow/overloaded), (c) what to do (retry). This matches the tone of the existing `ConnectionResetError` message.

---

## Edit 3 — Add tests for both the retry and the friendly message

Add these tests to `tests/test_llm_streaming.py`:

### 3a. Test that `friendly_error_message` handles `TimeoutError`

```python
def test_friendly_error_message_timeout():
    """TimeoutError produces a user-friendly message, not raw 'read operation timed out'."""
    from agent.llm.streaming import friendly_error_message
    exc = TimeoutError("The read operation timed out")
    msg = friendly_error_message(exc)
    assert "timed out" in msg.lower()
    assert "try" in msg.lower() or "again" in msg.lower()
    assert "read operation timed out" not in msg  # raw message should NOT be shown
```

### 3b. Test that `stream_with_ssl_retry` retries on `TimeoutError`

```python
def test_stream_with_ssl_retry_retries_on_timeout():
    """TimeoutError during streaming triggers a retry (not an immediate raise)."""
    import socket
    from agent.llm.streaming import stream_with_ssl_retry

    call_count = 0
    def flaky_streamer(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise TimeoutError("The read operation timed out")
        yield from []  # succeed on retry

    events = list(stream_with_ssl_retry(flaky_streamer, base_url="", api_key="", model="", messages=[], tools=None, timeout=1.0, x_title=""))
    assert call_count == 2, f"Expected 2 attempts (1 fail + 1 succeed), got {call_count}"
```

### 3c. Test that `stream_with_ssl_retry` raises after exhausting retries

```python
def test_stream_with_ssl_retry_raises_after_timeout_retries_exhausted():
    """TimeoutError persists across all retries → raises to caller."""
    from agent.llm.streaming import stream_with_ssl_retry, MAX_SSL_RETRIES

    def always_timeout(**kwargs):
        raise TimeoutError("The read operation timed out")

    with pytest.raises(TimeoutError):
        list(stream_with_ssl_retry(always_timeout, base_url="", api_key="", model="", messages=[], tools=None, timeout=1.0, x_title=""))
```

---

## Verification

1. `grep -n "TimeoutError" agent/llm/streaming.py` — expect ≥ 5 matches (RETRYABLE_OSERROR_TYPES + 2 except clauses + friendly_error_message + possibly docstring)
2. `python3 -c "from agent.llm.streaming import friendly_error_message; print(friendly_error_message(TimeoutError('timed out')))"` — prints a user-friendly message, not the raw error
3. `python3 -m pytest tests/test_llm_streaming.py -v` — all tests pass (12 existing + 3 new)

## COMPLETENESS checklist (mandatory)
```
COMPLETENESS:
- [x/not done] Edit 1: Verified TimeoutError in retry layers — evidence: <grep>
- [x/not done] Edit 2: Added TimeoutError to friendly_error_message — evidence: <python output>
- [x/not done] Edit 3a: test_friendly_error_message_timeout — evidence: <pytest>
- [x/not done] Edit 3b: test_stream_with_ssl_retry_retries_on_timeout — evidence: <pytest>
- [x/not done] Edit 3c: test_stream_with_ssl_retry_raises_after_timeout_retries_exhausted — evidence: <pytest>
- [x/not done] All tests pass — evidence: <pytest tail>
```
