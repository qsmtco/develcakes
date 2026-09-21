# Phase 1 Instructions — Truncated Streaming Tool-Call Args (Layer 1)

**Spec:** `docs/specs/SPEC-TRUNCATED-STREAMING-TOOL-ARGS.md`
**Builder:** Coder
**Rule:** Load `prompts/steelFramedCodeWriter.md` and follow every rule.
**Word marker:** please write

## Scope

ONE file change + ONE test file change. Do not touch `agent/runtime.py` — that is Phase 2.

### Files to change

1. `agent/llm/extractors.py` — wrap `json.loads(args_raw)` in `extract_tool_calls` with try/except
2. `tests/test_llm_extractors.py` — rewrite one test, add one test

## Edit 1: `agent/llm/extractors.py` — defensive chokepoint

**Location:** inside `extract_tool_calls`, in the OpenAI-format branch, in the `for tc in message["tool_calls"]:` loop. Current code (lines ~48-54):

```python
                call_id = tc.get("id") or f"call_{uuid.uuid4().hex[:8]}"
                name = func.get("name", "")
                args_raw = func.get("arguments", "{}")
                if isinstance(args_raw, str):
                    args = json.loads(args_raw)
                else:
                    args = args_raw or {}
                calls.append((call_id, name, args))
```

**Change:** Wrap the `json.loads(args_raw)` call in try/except. On `json.JSONDecodeError`:
- Log a warning with the tool name and a truncated preview of `args_raw` (first 200 chars — do NOT dump potentially huge/garbage strings in full).
- `continue` the loop (skip this tool call — do not append to `calls`).

```python
                call_id = tc.get("id") or f"call_{uuid.uuid4().hex[:8]}"
                name = func.get("name", "")
                args_raw = func.get("arguments", "{}")
                if isinstance(args_raw, str):
                    try:
                        args = json.loads(args_raw)
                    except json.JSONDecodeError:
                        # Defensive: malformed/truncated tool-call arguments
                        # (e.g. a streaming provider dropped the connection
                        # mid-arguments without sending [DONE]). Skip this
                        # tool call rather than crashing the agent turn.
                        logger.warning(
                            "[extract] skipping tool call with malformed "
                            "JSON arguments: tool=%s args_raw=%.200r",
                            name, args_raw,
                        )
                        continue
                else:
                    args = args_raw or {}
                calls.append((call_id, name, args))
```

**Do NOT change the Anthropic-format branch.** Anthropic arguments arrive as a parsed dict (`block.get("input", {})`), never a raw string, so there is no `json.loads` to guard there. Leaving it untouched keeps the change minimal.

**Why the `%.200r` format spec:** `args_raw` could be a multi-kilobyte garbage string from a corrupt stream. The `.200` precision cap on `%r` truncates the repr to 200 chars so the log line stays bounded. This is the same defensive-logging pattern used elsewhere in the runtime (e.g. `_call_llm` error logging).

### Edit 2: `tests/test_llm_extractors.py` — rewrite + add

**Location:** `TestExtractToolCalls` class.

**Edit 2a (rewrite the existing crash-assertion test):** The existing test `test_extract_tool_calls_malformed_json_args_raises` currently asserts the OLD behavior (raise). Replace its ENTIRE body with a new test that asserts the NEW graceful-degradation behavior. Keep the same test method name so the rename does not leave a dangling reference — or rename to `test_extract_tool_calls_malformed_json_args_skipped`. Rename is preferred for clarity.

Old test to find and replace verbatim:

```python
    def test_extract_tool_calls_malformed_json_args_raises(self):
        """Malformed JSON string arguments → json.loads raises (verbatim behavior)."""
        response = {
            "choices": [{"message": {"tool_calls": [
                {"id": "c1", "function": {"name": "x", "arguments": "not-json"}},
            ]}}],
        }
        with pytest.raises(json.JSONDecodeError):
            extract_tool_calls(response, response_format="openai")
```

New test (replacement):

```python
    def test_extract_tool_calls_malformed_json_args_skipped(self):
        """Malformed JSON string arguments → tool call skipped (not raised).

        Regression: deepseek streaming can drop a connection mid-tool-call
        without sending [DONE], producing truncated JSON arguments. The
        extractor must skip the malformed call rather than raise and kill
        the agent turn. See docs/specs/SPEC-TRUNCATED-STREAMING-TOOL-ARGS.md.
        """
        response = {
            "choices": [{"message": {"tool_calls": [
                {"id": "c1", "function": {"name": "x", "arguments": "not-json"}},
            ]}}],
        }
        calls = extract_tool_calls(response, response_format="openai")
        assert calls == []
```

**Edit 2b (add a mixed-response test):** Add a new test verifying that a valid tool call alongside a malformed one yields only the valid call. Place it immediately after `test_extract_tool_calls_malformed_json_args_skipped`:

```python
    def test_extract_tool_calls_mixed_valid_and_malformed_args(self):
        """One valid + one malformed tool call → only the valid call returned."""
        response = {
            "choices": [{"message": {"tool_calls": [
                {"id": "good", "function": {"name": "read_file",
                 "arguments": '{"path": "ok.py"}'}},
                {"id": "bad", "function": {"name": "exec_command",
                 "arguments": '{"command": "git sta'}},
            ]}}],
        }
        calls = extract_tool_calls(response, response_format="openai")
        assert len(calls) == 1
        assert calls[0][0] == "good"
        assert calls[0][1] == "read_file"
        assert calls[0][2] == {"path": "ok.py"}
```

**Note on imports:** `json` and `pytest` are already imported at the top of the test file. No new imports needed. If the rewrite of Edit 2a removes the last use of `pytest.raises`, you MAY leave the `import pytest` in place (other tests in the file may use it, and it is harmless) — do NOT remove it preemptively.

## Verification

Run (paste the full output):

```bash
python3 -m pytest tests/test_llm_extractors.py -v
```

All 12 tests must pass (11 existing, with 1 rewritten + 1 added = 12 total). The rewritten test must NOT raise.

Run a pattern sweep (paste output):

```bash
grep -n "json.loads(args_raw)" agent/llm/extractors.py
grep -n "test_extract_tool_calls_malformed" tests/test_llm_extractors.py
```

Expected:
- `json.loads(args_raw)` appears exactly once, now inside a try block.
- The old `_raises` test name is gone; `_skipped` and `_mixed_valid_and_malformed_args` are present.

## Deliverables — COMPLETENESS checklist (mandatory)

At the end of your response, include:

```
COMPLETENESS:
- [x/not done] Edit 1: extract_tool_calls json.loads wrapped in try/except — evidence: [grep line + the try/except lines]
- [x/not done] Edit 2a: test_extract_tool_calls_malformed_json_args_raises rewritten to _skipped — evidence: [grep output]
- [x/not done] Edit 2b: test_extract_tool_calls_mixed_valid_and_malformed_args added — evidence: [grep output]
- [x/not done] pytest tests/test_llm_extractors.py -v — evidence: [paste full output]
- [x/not done] No changes to agent/runtime.py — evidence: [git status or git diff --name-only]
```

A missing COMPLETENESS block is a missing deliverable — the delegation will be sent back.

## Out of scope (Phase 2 — do NOT do these now)

- `agent/runtime.py` `_call_llm_streaming` fallback validation
- `tests/test_agent_runtime.py` streaming-fallback tests
- Any change to the Anthropic branch in `extract_tool_calls`
