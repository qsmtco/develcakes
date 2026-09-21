# Phase 8 Instructions — Re-export Alias Cleanup

**Spec:** `docs/specs/SPEC-RUNTIME-EXTRACTION-PHASE-8.md` (revised after audit)
**Files:** `agent/runtime.py` + `tests/test_agent_runtime.py`

**FIRST ACTION: Load the steelFramedCodeWriter prompt fresh.** Read `/path/to/projects/crabcakes/prompts/steelFramedCodeWriter.md` IN FULL. Activate it. Begin with Discovery Phase block.

Read `agent/runtime.py` in full before editing — especially the re-export blocks at lines ~177-270 and ALL call sites of the underscored names.

---

## CRITICAL SCOPE NOTE

**DO NOT touch `_PROVIDER_CALLERS`, `_PROVIDER_STREAMERS`, `_RESPONSE_FORMAT`, `_call_openai`, `_call_minimax`, `_call_anthropic`, or `_get_provider`.** These are active dispatch infrastructure, NOT pure aliases. They are DEFERRED to a future phase. The spec audit confirmed removing them would break runtime dispatch.

**Only remove these 3 PURE ALIAS blocks:**
1. Converters: `_convert_messages_for_anthropic`, `_convert_tools_for_anthropic`
2. Streaming: `_sse_lines`, `_parse_sse_line`, `_parse_sse_delta`, `_first_choice`, `_urlopen_with_ssl_retry`, `_stream_with_ssl_retry`, `_is_retryable_ssl_error`, `_friendly_error_message`, + 4 constants (`_RETRYABLE_SSL_ERRORS`, `_RETRYABLE_OSERROR_TYPES`, `_MAX_SSL_RETRIES`, `_SSL_RETRY_BASE_MS`)
3. Extractors: `_extract_tool_calls`, `_extract_text_content`, `_extract_usage`

---

## Edit 1 — Converters block

Find the re-export block (~lines 177-182):
```python
# ── Anthropic converters (extracted to agent/llm/convert.py, Phase B2) ──────
# Re-exported under legacy underscore names for backward compatibility.
from agent.llm.convert import (
    convert_messages_for_anthropic as _convert_messages_for_anthropic,
    convert_tools_for_anthropic as _convert_tools_for_anthropic,
)
```

**Remove it.** Add a direct import at the top of the file (near other `from agent.llm.` imports):
```python
from agent.llm.convert import convert_messages_for_anthropic, convert_tools_for_anthropic
```

**Update call sites:** grep for `_convert_messages_for_anthropic` and `_convert_tools_for_anthropic` in runtime.py. Replace ALL with non-underscored names.

## Edit 2 — Streaming block

Find the re-export block (~lines 245-262). **Remove it.** Add a direct import at the top:
```python
from agent.llm.streaming import (
    SSEEvent,
    sse_lines,
    parse_sse_line,
    parse_sse_delta,
    first_choice,
    urlopen_with_ssl_retry,
    stream_with_ssl_retry,
    is_retryable_ssl_error,
    friendly_error_message,
    RETRYABLE_SSL_ERRORS,
    RETRYABLE_OSERROR_TYPES,
    MAX_SSL_RETRIES,
    SSL_RETRY_BASE_MS,
)
```

**Update call sites:** grep for each `_X` streaming symbol in runtime.py. Replace ALL with non-underscored names.

## Edit 3 — Extractors block

Find the re-export block (~lines 264-270):
```python
from agent.llm.extractors import (
    extract_tool_calls as _extract_tool_calls,
    extract_text_content as _extract_text_content,
    extract_usage as _extract_usage,
)
```

**Remove it.** Add a direct import at the top:
```python
from agent.llm.extractors import extract_tool_calls, extract_text_content, extract_usage
```

**Update call sites:** grep for `_extract_tool_calls`, `_extract_text_content`, `_extract_usage`. Replace ALL with non-underscored names.

## Edit 4 — Update `__all__`

Remove these from `__all__`:
- `_extract_tool_calls`
- `_extract_text_content`
- `_extract_usage`
- `_is_retryable_ssl_error`
- `_stream_with_ssl_retry`
- `_friendly_error_message`

**Do NOT remove** `_PROVIDER_CALLERS`, `_PROVIDER_STREAMERS` from `__all__` — they stay (deferred).

## Edit 5 — Update test patch targets

Grep for patch targets on the 3 in-scope blocks:
```bash
grep -n 'patch("agent\.runtime\._extract_\|patch("agent\.runtime\._sse_\|patch("agent\.runtime\._parse_sse\|patch("agent\.runtime\._stream_with_ssl\|patch("agent\.runtime\._urlopen_with_ssl\|patch("agent\.runtime\._convert_\|patch("agent\.runtime\._first_choice\|patch("agent\.runtime\._friendly' tests/test_agent_runtime.py
```

Update each from `agent.runtime._X` to the new module path (`agent.llm.extractors.X`, `agent.llm.streaming.X`, `agent.llm.convert.X`).

**Do NOT touch** patches on `_PROVIDER_CALLERS`, `_PROVIDER_STREAMERS`, `_call_openai`, `_call_minimax` — those are deferred.

---

## Verification

1. `grep -c "Re-exported under legacy" agent/runtime.py` → **0**
2. `grep -c "_convert_messages_for_anthropic\|_convert_tools_for_anthropic" agent/runtime.py` → **0**
3. `grep -c "_extract_tool_calls\|_extract_text_content\|_extract_usage" agent/runtime.py` → **0**
4. `grep -c "_sse_lines\|_parse_sse_line\|_parse_sse_delta\|_first_choice\|_urlopen_with_ssl_retry\|_stream_with_ssl_retry\|_is_retryable_ssl_error\|_friendly_error_message" agent/runtime.py` → **0**
5. `_PROVIDER_CALLERS` and `_PROVIDER_STREAMERS` still present (DEFERRED — verify still there)
6. `python3 -c "from agent.runtime import AgentRuntime; print('OK')"` → OK
7. `python3 -m pytest tests/test_agent_runtime.py -q` → all pass (use full file)
8. `python3 -m pytest tests/test_llm_providers.py tests/test_llm_streaming.py tests/test_llm_convert.py tests/test_llm_extractors.py -q` → all pass

## COMPLETENESS checklist (mandatory)
```
COMPLETENESS:
- [x/not done] Edit 1: Converters block removed, call sites updated — evidence: <grep -c = 0>
- [x/not done] Edit 2: Streaming block removed, call sites updated — evidence: <grep -c = 0>
- [x/not done] Edit 3: Extractors block removed, call sites updated — evidence: <grep -c = 0>
- [x/not done] Edit 4: __all__ cleaned (6 aliases removed, dispatch dicts kept) — evidence: <python>
- [x/not done] Edit 5: Test patch targets updated — evidence: <grep>
- [x/not done] Dispatch dicts preserved — evidence: <grep showing _PROVIDER_CALLERS still present>
- [x/not done] All tests pass — evidence: <pytest>
```
