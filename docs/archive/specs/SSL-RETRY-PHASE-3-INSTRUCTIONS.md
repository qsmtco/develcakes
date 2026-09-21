# PHASE 3 of 3 — Verification + Audit

**Spec:** `docs/specs/SPEC-SSL-RETRY-FIX.md`
**Depends on:** Phase 1 + Phase 2 complete

## What to do

### Verification 1: Inline acceptance tests

Run this Python script and paste the FULL output:
```bash
python3 -c "
import ssl, urllib.error
from agent.runtime import _is_retryable_ssl_error, _friendly_error_message, _RETRYABLE_SSL_ERRORS

# AC-1
assert _is_retryable_ssl_error(ssl.SSLEOFError('EOF occurred in violation of protocol'))
# AC-2
assert _is_retryable_ssl_error(urllib.error.URLError(ssl.SSLEOFError('EOF occurred in violation of protocol')))
# AC-3
assert not _is_retryable_ssl_error(urllib.error.URLError('DNS failure'))
# AC-4
assert _is_retryable_ssl_error(ConnectionResetError('reset'))
# AC-5
assert not _is_retryable_ssl_error(ssl.SSLError('SSLV3_ALERT_CERTIFICATE_UNKNOWN'))
# AC-6
msg = _friendly_error_message(ssl.SSLEOFError('EOF occurred in violation of protocol'))
assert 'Connection to the AI provider was lost' in msg, f'Got: {msg}'
# AC-7
assert _friendly_error_message(ValueError('bad')) == 'bad'
print('All 7 acceptance criteria passed!')
"
```

### Verification 2: Full test suite

Run:
```bash
python3 -m pytest tests/ -q --tb=short --deselect "tests/test_agent_runtime.py::TestApproval::test_exec_with_approval_allow" --deselect "tests/test_agent_runtime.py::TestApproval::test_exec_with_approval_deny" 2>&1 | tail -10
```

### Verification 3: Syntax check
```bash
python3 -c "import ast; ast.parse(open('agent/runtime.py').read()); print('Syntax OK')"
```

### Verification 4: Confirm all three functions are in `__all__`
```bash
python3 -c "from agent import runtime; assert '_stream_with_ssl_retry' in runtime.__all__; assert '_is_retryable_ssl_error' in runtime.__all__; assert '_friendly_error_message' in runtime.__all__; print('All 3 in __all__')"
```

## COMPLETENESS checklist required

Please write Phase 3 when ready.
