# Phase 8 Audit Fix — TestStreaming mock migration

**Spec:** Phase 8 audit BUG #1
**File:** `tests/test_agent_runtime.py` only

4 TestStreaming tests use the legacy `_PROVIDER_STREAMERS["openai"]` patch pattern which is dead (production code uses `_get_provider(caller_key).stream` since Phase B6). The tests make real HTTPS calls and fail with 401.

**FIRST ACTION: Load the steelFramedCodeWriter prompt fresh.** Read `/path/to/projects/crabcakes/prompts/steelFramedCodeWriter.md` IN FULL. Activate it.

Read `tests/test_agent_runtime.py` lines 1395-1545 (the TestStreaming class) in full before editing.

---

## The fix pattern

Replace the legacy pattern:
```python
from agent import runtime as rt_module
orig = rt_module._PROVIDER_STREAMERS["openai"]
rt_module._PROVIDER_STREAMERS["openai"] = lambda *a, **kw: mock_streamer()
try:
    with unittest.mock.patch.object(rt, "_call_llm", _make_streaming_lambda(rt)):
        rt._run_loop(sk, "...")
finally:
    rt_module._PROVIDER_STREAMERS["openai"] = orig
```

With the modern pattern (matching the 3 passing tests in the same class):
```python
from unittest.mock import MagicMock
mock_provider = MagicMock()
mock_provider.stream.return_value = mock_streamer()
with unittest.mock.patch("agent.runtime._get_provider", return_value=mock_provider):
    with unittest.mock.patch.object(rt, "_call_llm", _make_streaming_lambda(rt)):
        rt._run_loop(sk, "...")
```

The key insight: `_call_llm_streaming` calls `_get_provider(caller_key).stream(**kwargs)`. The mock provider's `.stream` must return an iterator of SSEEvent objects (the same events the old lambda returned).

---

## The 4 tests to fix

### Test 1: `test_response_complete_fires_after_stream` (line ~1435)

Old mock: `lambda *a, **kw: _mock_stream_openai_3_chunks()`
New: `mock_provider.stream.return_value = _mock_stream_openai_3_chunks()`

### Test 2: `test_tool_call_start_fires_when_complete` (line ~1458)

Old mock: `lambda *a, **kw: _mock_stream_with_tool_call()`
New: `mock_provider.stream.return_value = _mock_stream_with_tool_call()`

### Test 3: `test_streaming_accumulates_text_in_response` (line ~1490)

Old mock: `lambda *a, **kw: _mock_stream_openai_3_chunks()`
New: `mock_provider.stream.return_value = _mock_stream_openai_3_chunks()`

### Test 4: `test_tool_call_delta_without_index_defaults_to_zero` (line ~1513)

Old mock: a local `streamer_no_index` generator function
New: `mock_provider.stream.return_value = streamer_no_index()` (call it to get the generator)

**IMPORTANT for Test 4:** The old code defined `streamer_no_index` as a generator function and assigned it directly. The mock provider's `.stream` must receive the generator OBJECT (call the function). If `streamer_no_index` is a generator function, call it: `mock_provider.stream.return_value = streamer_no_index()`.

---

## Verification

1. `python3 -m pytest tests/test_agent_runtime.py::TestStreaming -v` → 7/7 pass (was 4 failed, 3 passed)
2. `grep -c "_PROVIDER_STREAMERS" tests/test_agent_runtime.py` → should be 0 in TestStreaming class (may appear in docstrings/comments elsewhere — that's fine)
3. No real network calls (all mocked)

## COMPLETENESS checklist (mandatory)
```
COMPLETENESS:
- [x/not done] Test 1: test_response_complete_fires_after_stream — modern mock — evidence: <pytest>
- [x/not done] Test 2: test_tool_call_start_fires_when_complete — modern mock — evidence: <pytest>
- [x/not done] Test 3: test_streaming_accumulates_text_in_response — modern mock — evidence: <pytest>
- [x/not done] Test 4: test_tool_call_delta_without_index_defaults_to_zero — modern mock — evidence: <pytest>
- [x/not done] All 7 TestStreaming tests pass — evidence: <pytest 7/7>
```
