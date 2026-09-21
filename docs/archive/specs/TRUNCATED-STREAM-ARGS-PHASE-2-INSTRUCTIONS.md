# Phase 2 Instructions — Truncated Streaming Tool-Call Args (Layer 2)

**Spec:** `docs/specs/SPEC-TRUNCATED-STREAMING-TOOL-ARGS.md`
**Builder:** Coder
**Rule:** Load `prompts/steelFramedCodeWriter.md` and follow every rule.
**Word marker:** please write
**Prerequisite:** Phase 1 complete and verified clean.

## Scope

ONE file change + ONE test file addition. Phase 1 already hardened `extract_tool_calls` (the downstream chokepoint). Phase 2 hardens the **source** — the streaming assembly in `_call_llm_streaming`.

### Files to change

1. `agent/runtime.py` — validate accumulated arguments in BOTH the `done`-event path and the fallback (no-done-event) path of `_call_llm_streaming`
2. `tests/test_agent_runtime.py` — add tests for both paths

## Context — the two code paths

Both paths live inside `_call_llm_streaming` (starts ~line 1580). They have **identical** accumulation + emission logic. The `done`-event path fires when the provider sends an explicit `[DONE]` sentinel; the fallback fires when the stream exhausts naturally without it.

### Current done-event path (~lines 1631-1648)

```python
            elif ev.type == "done":
                tool_calls = []
                for idx in sorted(tool_calls_partial.keys()):
                    tc = tool_calls_partial[idx]
                    if tc["name"]:
                        tool_calls.append({
                            "id": tc["id"] or f"call_{idx}",
                            "function": {
                                "name": tc["name"],
                                "arguments": tc["arguments"]
                            }
                        })
                logger.debug(...)
                return {...}
```

### Current fallback path (~lines 1652-1663)

```python
        # Fallback — stream ended without explicit done event
        tool_calls = []
        for idx in sorted(tool_calls_partial.keys()):
            tc = tool_calls_partial[idx]
            if tc["name"]:
                tool_calls.append({
                    "id": tc["id"] or f"call_{idx}",
                    "function": {
                        "name": tc["name"],
                        "arguments": tc["arguments"]
                    }
                })
        logger.debug(...)
        return {...}
```

Both append `tc["arguments"]` (a raw concatenated string of SSE fragments) with **no JSON validation**.

---

## Edit 1: Add a module-level validation helper

To avoid duplicating the try/except in two places, add a small private helper function near the top of `_call_llm_streaming` (or as a module-level function just above the `AgentRuntime` class — your choice, but module-level is cleaner for testing).

**Location:** Place it as a module-level function, just above the `class AgentRuntime:` definition (or near the other module-level helpers if there's a cluster of them). Name it `_validate_streamed_arguments`.

```python
def _validate_streamed_arguments(
    args_str: str, tool_name: str, session_key: str
) -> bool:
    """Validate that accumulated streaming arguments form complete JSON.

    Returns True if the arguments are valid JSON (or empty — empty is
    allowed, some providers send arguments as a separate frame that may
    not have arrived). Returns False if the arguments are malformed
    (truncated stream), in which case the caller should skip the tool call.

    Logs a warning on failure so truncated streams are observable.
    """
    if not args_str:
        return True  # empty is valid — no arguments fragment arrived yet
    try:
        json.loads(args_str)
        return True
    except json.JSONDecodeError:
        logger.warning(
            "[stream] sk=%s skipping tool=%s with incomplete JSON arguments "
            "(stream truncated): %.200r",
            session_key, tool_name, args_str,
        )
        return False
```

**Import check:** `json` and `logger` are already imported at the top of `agent/runtime.py`. Verify with:
```bash
grep -n "^import json" agent/runtime.py
grep -n "^logger = " agent/runtime.py
```

## Edit 2: Call the helper in the done-event path

In the `done`-event path, inside the `if tc["name"]:` block, add a validation call **before** appending. Skip the tool call if validation fails.

Change from:
```python
                    if tc["name"]:
                        tool_calls.append({
```

To:
```python
                    if tc["name"] and _validate_streamed_arguments(
                        tc["arguments"], tc["name"], session_key
                    ):
                        tool_calls.append({
```

**IMPORTANT — uniqueness for the edit:** The `if tc["name"]:` line appears in BOTH the done-event path and the fallback path. They are textually identical. When making this edit, include enough surrounding context (the `elif ev.type == "done":` line above) so the edit targets the correct occurrence. If using a text-replace tool, the old_text MUST include the `elif ev.type == "done":` line to be unique.

## Edit 3: Call the helper in the fallback path

Same change in the fallback path. Change from:
```python
            tc = tool_calls_partial[idx]
            if tc["name"]:
                tool_calls.append({
```

To:
```python
            tc = tool_calls_partial[idx]
            if tc["name"] and _validate_streamed_arguments(
                tc["arguments"], tc["name"], session_key
            ):
                tool_calls.append({
```

**Uniqueness:** include the `# Fallback — stream ended without explicit done event` comment line in the old_text to disambiguate from the done-event path.

---

## Edit 4: Tests — `tests/test_agent_runtime.py`

Add a new test class `TestStreamedArgumentsValidation` with at least these tests. These test the helper directly AND the integration through `_call_llm_streaming` (mocked stream).

### Test 4a: helper validates good JSON

```python
class TestStreamedArgumentsValidation:
    """Tests for _validate_streamed_arguments and _call_llm_streaming
    truncation handling. See docs/specs/SPEC-TRUNCATED-STREAMING-TOOL-ARGS.md."""

    def test_validate_good_json(self):
        from agent.runtime import _validate_streamed_arguments
        assert _validate_streamed_arguments('{"path": "x.py"}', "read_file", "sk1") is True

    def test_validate_empty_string(self):
        from agent.runtime import _validate_streamed_arguments
        assert _validate_streamed_arguments("", "read_file", "sk1") is True

    def test_validate_malformed_json(self):
        from agent.runtime import _validate_streamed_arguments
        assert _validate_streamed_arguments('{"command": "git sta', "exec_command", "sk1") is False
```

### Test 4b: integration — fallback path skips malformed tool call

This test exercises `_call_llm_streaming` with a mocked `stream_with_ssl_retry` that yields fragments WITHOUT a done event (forcing the fallback path), and asserts that malformed accumulated arguments are skipped.

The test must:
1. Mock the provider's `.stream` method (or `stream_with_ssl_retry`) to yield `tool_call_delta` events that build a truncated argument string, then end without `done`.
2. Call `_call_llm_streaming` (or the public path that reaches it).
3. Assert the returned response dict's `tool_calls` list excludes the malformed call.

**Study the existing streaming tests first.** Before writing this test, read how existing tests in `test_agent_runtime.py` mock `_call_llm_streaming` / `stream_with_ssl_retry`. Look for `TestStreaming` or tests that patch `agent.llm.streaming.stream_with_ssl_retry` or the provider's `.stream` method. Mirror that mocking pattern exactly.

If the existing streaming tests are too complex to mirror safely, you may test the helper + the emission logic in isolation: construct a `tool_calls_partial` dict with a malformed entry, call the validation inline, and assert the emitted list excludes it. Document which approach you took in the COMPLETENESS checklist.

---

## Verification

```bash
python3 -m pytest tests/test_agent_runtime.py::TestStreamedArgumentsValidation -v
python3 -m pytest tests/test_agent_runtime.py -v --tb=short -q
python3 -m pytest tests/test_llm_extractors.py -v   # Phase 1 regression — must still pass
```

Pattern sweep (paste output):
```bash
grep -n "_validate_streamed_arguments" agent/runtime.py
grep -n "def _validate_streamed_arguments" agent/runtime.py
grep -c "json.loads" agent/runtime.py   # should NOT have added raw json.loads calls in the streaming paths
```

## Deliverables — COMPLETENESS checklist (mandatory)

```
COMPLETENESS:
- [x/not done] Edit 1: _validate_streamed_arguments helper added — evidence: [grep output]
- [x/not done] Edit 2: done-event path calls helper — evidence: [diff lines]
- [x/not done] Edit 3: fallback path calls helper — evidence: [diff lines]
- [x/not done] Edit 4: tests added — evidence: [pytest output]
- [x/not done] pytest tests/test_agent_runtime.py -v — evidence: [paste summary line]
- [x/not done] pytest tests/test_llm_extractors.py -v (Phase 1 regression) — evidence: [paste summary line]
```

A missing COMPLETENESS block is a missing deliverable — the delegation will be sent back.
