# PHASE 1 of 3 — Layer 1: Fix `_urlopen_with_ssl_retry` + helpers

**Spec:** `docs/specs/SPEC-SSL-RETRY-FIX.md` (read this first)

## Files to change

**ONLY:** `agent/runtime.py`

## What to do

### Edit 1: Add `import urllib.error`

Near the existing `import ssl` (around line 444), add:
```python
import urllib.error
```

### Edit 2: Expand `_RETRYABLE_SSL_ERRORS`

Add two new tokens to the existing frozenset:
```python
_RETRYABLE_SSL_ERRORS = frozenset({
    "SSLV3_ALERT_BAD_RECORD_MAC",
    "SSLV3_ALERT_BAD_RECORD_MD5",
    "TLSV1_ALERT_DECRYPTION_FAILED",
    "TLSV1_ALERT_RECORD_OVERFLOW",
    "SSL_ERROR_SYSCALL",
    "EOF occurred in violation of protocol",
    "UNEXPECTED_EOF_WHILE_READING",
})
```

### Edit 3: Add `_RETRYABLE_OSERROR_TYPES`

After `_RETRYABLE_SSL_ERRORS`, add:
```python
_RETRYABLE_OSERROR_TYPES: tuple[type[Exception], ...] = (
    ConnectionResetError,
    BrokenPipeError,
)
```

### Edit 4: Add `_is_retryable_ssl_error()` function

After the constants, before `_urlopen_with_ssl_retry`. The function must:
- Accept `exc: BaseException`
- Build a candidates list: `[exc]`, plus `exc.reason` if it's a `URLError`, plus `exc.__cause__` if present
- For each candidate: if it's an `ssl.SSLError`, check if `str(cand)` contains any token from `_RETRYABLE_SSL_ERRORS`
- For each candidate: if it's an instance of `_RETRYABLE_OSERROR_TYPES`, return True
- Return False otherwise

### Edit 5: Rewrite `_urlopen_with_ssl_retry`

The existing function catches only `ssl.SSLError`. Rewrite it to:
1. `except ssl.SSLError` — use `_is_retryable_ssl_error(e)` to decide retry
2. `except urllib.error.URLError` — use `_is_retryable_ssl_error(e)` (this is the KEY BUG fix — `do_open` wraps `OSError` in `URLError`)
3. `except _RETRYABLE_OSERROR_TYPES` — retry on TCP resets
4. Each branch: if not retryable or max attempts reached, re-raise
5. Otherwise, log warning and sleep with exponential backoff

### Edit 6: Append to `__all__`

Add `"_is_retryable_ssl_error"` to the `__all__` list.

## Rules

- Use the `steelFramedCodeWriter` prompt at `prompts/steelFramedCodeWriter.md`
- READ ALL FILES BEFORE STARTING — read `agent/runtime.py` lines 440-680 to see the existing SSL retry code
- Run: `python3 -c "import ast; ast.parse(open('agent/runtime.py').read()); print('Syntax OK')"`
- Run: `python3 -m pytest tests/test_agent_runtime.py -q --tb=short --deselect "tests/test_agent_runtime.py::TestApproval::test_exec_with_approval_allow" --deselect "tests/test_agent_runtime.py::TestApproval::test_exec_with_approval_deny" 2>&1 | tail -5`
- Paste ALL command output
- Report: files changed with line numbers, test results, any issues

## COMPLETENESS checklist required

At the end of your response, include:
```
COMPLETENESS:
- [x/not done] Edit 1: import urllib.error — evidence
- [x/not done] Edit 2: EOF tokens in _RETRYABLE_SSL_ERRORS — evidence
- [x/not done] Edit 3: _RETRYABLE_OSERROR_TYPES tuple — evidence
- [x/not done] Edit 4: _is_retryable_ssl_error() function — evidence
- [x/not done] Edit 5: Rewrite _urlopen_with_ssl_retry — evidence
- [x/not done] Edit 6: __all__ updated — evidence
```

Please write Phase 1 when ready.
