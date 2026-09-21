# Phase B5 Instructions — Extract SSE Helpers + SSL Retry to agent/llm/streaming.py

**Track:** B Phase 2b (streaming extraction — first half)
**Scope:** Create `agent/llm/streaming.py` (NEW), edit `agent/runtime.py` (re-export block), create `tests/test_llm_streaming.py` (NEW).
**Spec reference:** `docs/specs/SPEC-RUNTIME-MODULAR-EXTRACTION-PHASE-1.md` §B.3.5, §B.5
**Rule reference:** `prompts/steelFramedCodeWriter.md` — **READ THIS FILE IN FULL FIRST. It is mandatory.**

## STEP 0 (MANDATORY — do this before writing any code)

Read `prompts/steelFramedCodeWriter.md` in full. Your COMPLETENESS checklist must cite which Steel-Framed rules you applied.

## Objective

Extract the SSE parsing helpers and SSL retry infrastructure from `agent/runtime.py` into a new `agent/llm/streaming.py` module. This is the infrastructure layer that the stream functions (`_stream_openai_events`, etc.) depend on. The stream functions themselves stay in runtime.py until Phase B6.

**What moves:** SSEEvent namedtuple, _sse_lines, _parse_sse_line, _parse_sse_delta, all SSL retry constants/helpers, _urlopen_with_ssl_retry, _stream_with_ssl_retry, _friendly_error_message.

**What STAYS in runtime.py (NOT moved):** `_stream_openai_events` (~line 636), `_stream_minimax_events` (~line 714), `_stream_anthropic_events` (~line 825), `_PROVIDER_STREAMERS` dict (~line 928). These move in Phase B6.

## STEP 1: Discovery (mandatory per Steel-Framed Rule 1)

Read these files in full before writing any code:
1. `prompts/steelFramedCodeWriter.md` — your standing orders
2. `docs/specs/SPEC-RUNTIME-MODULAR-EXTRACTION-PHASE-1.md` §B.3.5 (streaming.py spec), §B.5 (re-exports)
3. `agent/runtime.py` lines 243-635 — the SSE block you are moving. Read ALL of it.
4. `agent/llm/cost.py`, `agent/llm/convert.py` — templates for how an extracted module looks

## Deliverable — 3 files

### File 1: `agent/llm/streaming.py` (NEW)

Move these symbols VERBATIM from `agent/runtime.py` (lines ~243-635):

| Old name (runtime.py) | New name (streaming.py) |
|---|---|
| `SSEEvent` | `SSEEvent` (keep name — it's already public per __all__) |
| `_sse_lines` | `sse_lines` |
| `_parse_sse_line` | `parse_sse_line` |
| `_parse_sse_delta` | `parse_sse_delta` |
| `_urlopen_with_ssl_retry` | `urlopen_with_ssl_retry` |
| `_stream_with_ssl_retry` | `stream_with_ssl_retry` |
| `_RETRYABLE_SSL_ERRORS` | `RETRYABLE_SSL_ERRORS` |
| `_MAX_SSL_RETRIES` | `MAX_SSL_RETRIES` |
| `_SSL_RETRY_BASE_MS` | `SSL_RETRY_BASE_MS` |
| `_is_retryable_ssl_error` | `is_retryable_ssl_error` |
| `_friendly_error_message` | `friendly_error_message` |

**Drop leading underscores** on all symbols except `SSEEvent` (which stays as-is).

**Required imports** (check the original code for what it uses):
- `import json` — used by parse_sse_line, parse_sse_delta
- `import logging` + `logger = logging.getLogger(__name__)` — used throughout
- `import ssl` — used by is_retryable_ssl_error, stream_with_ssl_retry
- `import time` — used by stream_with_ssl_retry (time.sleep)
- `import urllib.error` — used by urlopen_with_ssl_retry, stream_with_ssl_retry
- `import urllib.request` — used by urlopen_with_ssl_retry
- `from collections import namedtuple` — for SSEEvent
- `from typing import Iterator` — for sse_lines return type

**IMPORTANT — internal cross-references:** The function bodies reference each other with the OLD underscore names. After renaming, update all internal references:
- In `stream_with_ssl_retry`: `_RETRYABLE_SSL_ERRORS` → `RETRYABLE_SSL_ERRORS`, `_MAX_SSL_RETRIES` → `MAX_SSL_RETRIES`, `_SSL_RETRY_BASE_MS` → `SSL_RETRY_BASE_MS`, `_is_retryable_ssl_error` → `is_retryable_ssl_error`
- In `urlopen_with_ssl_retry`: `_MAX_SSL_RETRIES` → `MAX_SSL_RETRIES`, `_is_retryable_ssl_error` → `is_retryable_ssl_error`, `_friendly_error_message` → `friendly_error_message`
- In any function calling `_sse_lines` → `sse_lines`, `_parse_sse_line` → `parse_sse_line`, `_parse_sse_delta` → `parse_sse_delta`

### File 2: Edit `agent/runtime.py` — replace SSE block with re-exports

Find the SSE block (starts at `import ssl` / `import urllib.error` / `import urllib.request`, around line 243, and ends right before `def _stream_openai_events`, around line 636). Delete the entire block and replace with:

```python
# ── SSE streaming helpers (extracted to agent/llm/streaming.py, Phase B5) ──
# Re-exported under legacy underscore names for backward compatibility.
# SSEEvent stays public (already in __all__). Stream event functions
# (_stream_openai_events etc.) stay here — they move in Phase B6.
from agent.llm.streaming import (
    SSEEvent,
    sse_lines as _sse_lines,
    parse_sse_line as _parse_sse_line,
    parse_sse_delta as _parse_sse_delta,
    urlopen_with_ssl_retry as _urlopen_with_ssl_retry,
    stream_with_ssl_retry as _stream_with_ssl_retry,
    is_retryable_ssl_error as _is_retryable_ssl_error,
    friendly_error_message as _friendly_error_message,
    RETRYABLE_SSL_ERRORS as _RETRYABLE_SSL_ERRORS,
    MAX_SSL_RETRIES as _MAX_SSL_RETRIES,
    SSL_RETRY_BASE_MS as _SSL_RETRY_BASE_MS,
)

import ssl
import urllib.error
import urllib.request
```

**Key points:**
- The `import ssl`, `import urllib.error`, `import urllib.request` lines stay at the top of runtime.py (the stream functions `_stream_openai_events` etc. still use them directly).
- The re-export block replaces the old inline SSE code.
- The stream functions (`_stream_openai_events`, `_stream_minimax_events`, `_stream_anthropic_events`) and `_PROVIDER_STREAMERS` dict are NOT touched.

### File 3: `tests/test_llm_streaming.py` (NEW)

Spec §B.9 cases 41-50 (streaming tests).

**Test cases:**
1. `test_sse_lines_strips_whitespace` — lines stripped of whitespace
2. `test_parse_sse_line_data_prefix` — "data: {...}" → SSEEvent
3. `test_parse_sse_line_done` — "data: [DONE]" → done event
4. `test_parse_sse_line_comment` — ":comment" → None
5. `test_parse_sse_delta_text_content` — delta.content → text_delta event
6. `test_parse_sse_delta_tool_call` — delta.tool_calls → tool_call_delta event

**Sad-path tests:**
7. `test_parse_sse_line_malformed_json` — bad JSON → None (not crash)
8. `test_urlopen_ssl_retry_transient_error` — retryable SSL error retried
9. `test_urlopen_ssl_retry_non_retryable_raises` — non-retryable error raised immediately
10. `test_urlopen_ssl_retry_max_attempts` — exhausts retries then raises

**Backward-compat tests:**
11. `test_runtime_reexport_sse_event` — `from agent.runtime import SSEEvent` works
12. `test_runtime_reexport_stream_with_ssl_retry` — `from agent.runtime import _stream_with_ssl_retry` works

## Verification commands

```bash
# New module imports
python3 -c "from agent.llm.streaming import SSEEvent, sse_lines, parse_sse_line, parse_sse_delta, urlopen_with_ssl_retry, stream_with_ssl_retry; print('import OK')"

# runtime.py re-exports work
python3 -c "from agent.runtime import SSEEvent, _sse_lines, _parse_sse_line, _parse_sse_delta, _urlopen_with_ssl_retry, _stream_with_ssl_retry; print('re-export OK')"

# runtime.py compiles
python3 -c "from agent.runtime import AgentRuntime; print('runtime OK')"

# New streaming tests pass (expect 12)
python3 -m pytest tests/test_llm_streaming.py -v

# Old SSE defs gone from runtime.py
grep -c "^SSEEvent = namedtuple\|^def _sse_lines\|^def _parse_sse_line\|^def _parse_sse_delta\|^def _urlopen_with_ssl_retry\|^def _stream_with_ssl_retry" agent/runtime.py  # must be 0

# Stream functions STILL in runtime.py (must be 3)
grep -c "^def _stream_openai_events\|^def _stream_minimax_events\|^def _stream_anthropic_events" agent/runtime.py  # must be 3

# runtime.py line count
wc -l agent/runtime.py

# No collateral damage
git diff --name-only agent/tools.py agent/enforcement.py tests/test_agent_runtime.py  # must be empty
```

## COMPLETENESS checklist

```
COMPLETENESS:
- [x/not done] STEP 0: Read prompts/steelFramedCodeWriter.md — cite 3 rules
- [x/not done] agent/llm/streaming.py created with 11 public symbols — evidence: import
- [x/not done] Internal cross-references updated (old underscore → new public names) — evidence: grep
- [x/not done] agent/runtime.py SSE block replaced with re-exports — evidence: grep (0 old defs, 1 import block)
- [x/not done] Stream functions (_stream_openai/minimax/anthropic_events) NOT moved — evidence: grep count = 3
- [x/not done] tests/test_llm_streaming.py created with 12 tests — evidence: pytest count
- [x/not done] All new streaming tests pass — evidence: pytest summary
- [x/not done] runtime.py compiles + imports — evidence: python3 -c output
- [x/not done] runtime.py line count dropped — evidence: wc -l
- [x/not done] No collateral damage — evidence: git diff output
```

## Do NOT

- Do NOT move `_stream_openai_events`, `_stream_minimax_events`, `_stream_anthropic_events` — they stay in runtime.py (Phase B6).
- Do NOT move `_PROVIDER_STREAMERS` — stays in runtime.py (Phase B6).
- Do NOT modify the stream function bodies.
- Do NOT modify `tests/test_agent_runtime.py`.
- Do NOT change the SSE parsing / SSL retry logic — move verbatim (only rename: drop underscores, update internal refs).
