# Phase 3 Instructions — Audit Fix Round 1 (BUG #1 + BUG #2)

**Spec:** `docs/specs/SPEC-TRUNCATED-STREAMING-TOOL-ARGS.md`
**Builder:** Coder
**Rule:** Load `prompts/steelFramedCodeWriter.md` and follow every rule.
**Word marker:** please write
**Prerequisite:** Phases 1 + 2 complete and verified clean. This is a fix-up round for two audit findings.

## Scope

TWO one-line code fixes + tests for each. These address Debugger's Phase 2 audit findings.

### Files to change

1. `agent/llm/extractors.py` — one-line fix to BUG #1 (empty-args inter-layer inconsistency)
2. `agent/runtime.py` — one-line fix to BUG #2 (narrow except in `_validate_streamed_arguments`)
3. `tests/test_llm_extractors.py` — add test for BUG #1 fix
4. `tests/test_agent_runtime.py` — add test for BUG #2 fix

---

## BUG #1 — empty-args inter-layer inconsistency (medium)

### Problem (verified live)

A tool call with a name but empty `arguments` string (e.g. a zero-arg Anthropic tool) passes Phase 2's `_validate_streamed_arguments` (returns `True` for `""`), gets emitted by `_call_llm_streaming` with `{"function": {"name": "...", "arguments": ""}}`, then is **silently dropped** by Phase 1's `extract_tool_calls` because `json.loads("")` raises and the `continue` skips it.

**Root cause:** `agent/llm/extractors.py:51` uses `func.get("arguments", "{}")`. The `.get(key, default)` form only applies the default when the key is **missing**. The streaming code at `agent/runtime.py:1637` always populates `arguments` with `""` (never missing), so the default `"{}"` never fires for the empty case. An empty-but-present `arguments` slips past the defaulting and crashes `json.loads`.

### Fix — Edit 1: `agent/llm/extractors.py:51`

Change:
```python
                args_raw = func.get("arguments", "{}")
```
To:
```python
                args_raw = func.get("arguments") or "{}"
```

The `or "{}"` form applies the default to missing key, `None`, **and** empty string — aligning Phase 1's defaulting with Phase 2's empty-allow semantics. A name-only tool call now extracts as `{"name": "...", "args": {}}`.

**Verify the line before editing** — open the file and confirm `args_raw = func.get("arguments", "{}")` is present on the line (it was at line 51 in Phase 1; drift is possible).

### Test for BUG #1 — Edit 2: `tests/test_llm_extractors.py`

Add a new test in `TestExtractToolCalls` that locks down the corrected behavior. Place it after `test_extract_tool_calls_mixed_valid_and_malformed_args`:

```python
    def test_extract_tool_calls_empty_string_arguments_defaults_to_empty_dict(self):
        """BUG #1: empty-but-present arguments string defaults to {} (not dropped).

        Regression: a name-only tool call (zero args, common for Anthropic/MCP
        tools) used to be silently dropped because func.get("arguments", "{}")
        only defaults when the key is missing, not when it's an empty string.
        The streaming code always populates 'arguments' with '' (agent/runtime.py:1637),
        so the default never fired. Fix: func.get("arguments") or '{}'.
        See docs/specs/SPEC-TRUNCATED-STREAMING-TOOL-ARGS.md (Phase 3, BUG #1).
        """
        response = {
            "choices": [{"message": {"tool_calls": [
                {"id": "c0", "function": {"name": "clear_cache", "arguments": ""}},
            ]}}],
        }
        calls = extract_tool_calls(response, response_format="openai")
        assert len(calls) == 1
        assert calls[0][0] == "c0"
        assert calls[0][1] == "clear_cache"
        assert calls[0][2] == {}
```

---

## BUG #2 — narrow except in `_validate_streamed_arguments` (low)

### Problem (verified live)

`_validate_streamed_arguments` catches `json.JSONDecodeError` but `json.loads` also raises `TypeError` on non-string input (int, list, dict). Currently unreachable (the only caller, `_call_llm_streaming`, guarantees strings via line 1637), but the helper is module-level and a future caller could trip it.

### Fix — Edit 3: `agent/runtime.py` in `_validate_streamed_arguments`

Find the except clause in the helper (search for `except json.JSONDecodeError:` inside `_validate_streamed_arguments`). Change:
```python
    except json.JSONDecodeError:
```
To:
```python
    except (json.JSONDecodeError, TypeError):
```

**Uniqueness note:** The string `except json.JSONDecodeError:` may appear elsewhere in `runtime.py`. Confirm you are editing the occurrence **inside** `_validate_streamed_arguments` (around line 273). The `extractors.py` Phase 1 guard is a separate `except json.JSONDecodeError:` in a different file — do not touch it (Phase 1 handles the string-only contract there and should stay narrow).

### Test for BUG #2 — Edit 4: `tests/test_agent_runtime.py`

Add a test in `TestStreamedArgumentsValidation`:

```python
    def test_validate_non_string_input_returns_false(self):
        """BUG #2: non-string input (int) does not crash; returns False.

        json.loads raises TypeError on non-string input. The helper's except
        clause covers both JSONDecodeError and TypeError so a future caller
        passing a non-string does not crash the agent turn.
        """
        from agent.runtime import _validate_streamed_arguments
        # int input — previously crashed with TypeError
        assert _validate_streamed_arguments(42, "f", "sk") is False
```

---

## Verification

Run all (paste output):

```bash
python3 -m pytest tests/test_llm_extractors.py -v
python3 -m pytest tests/test_agent_runtime.py::TestStreamedArgumentsValidation -v
python3 -m pytest tests/test_agent_runtime.py::TestStreaming -v 2>&1 | tail -5
```

Pattern sweep (paste output):

```bash
grep -n 'func.get("arguments")' agent/llm/extractors.py
grep -n 'except (json.JSONDecodeError, TypeError)' agent/runtime.py
```

Expected:
- `func.get("arguments")` appears once (the fix); the old `func.get("arguments", "{}")` is gone from `extractors.py`
- `except (json.JSONDecodeError, TypeError)` appears once inside `_validate_streamed_arguments` (the Phase 1 guard in `extractors.py` stays as `except json.JSONDecodeError:` — narrow on purpose)

## Deliverables — COMPLETENESS checklist (mandatory)

```
COMPLETENESS:
- [x/not done] Edit 1: extractors.py func.get("arguments") or "{}" — evidence: [grep line]
- [x/not done] Edit 2: test_extract_tool_calls_empty_string_arguments_defaults_to_empty_dict — evidence: [pytest output]
- [x/not done] Edit 3: runtime.py except (json.JSONDecodeError, TypeError) — evidence: [grep line]
- [x/not done] Edit 4: test_validate_non_string_input_returns_false — evidence: [pytest output]
- [x/not done] No regressions — evidence: [TestExtractToolCalls + TestStreamedArgumentsValidation + TestStreaming summaries]
```

A missing COMPLETENESS block is a missing deliverable — the delegation will be sent back.
