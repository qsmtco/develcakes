# Phase B6 Instructions — Move Stream Functions into Provider Classes

**Track:** B Phase 2b (streaming extraction — FINAL code extraction)
**Scope:** Edit `agent/llm/openai_provider.py`, `agent/llm/minimax_provider.py`, `agent/llm/anthropic_provider.py` (add `stream()` methods), edit `agent/runtime.py` (replace stream function defs + _PROVIDER_STREAMERS with re-exports), add streaming tests to `tests/test_llm_providers.py`.
**Spec reference:** `docs/specs/SPEC-RUNTIME-MODULAR-EXTRACTION-PHASE-1.md` §B.3.2, §B.3.3, §B.3.4, §B.4.2, §B.5, §B.6
**Rule reference:** `prompts/steelFramedCodeWriter.md` — **READ THIS FILE IN FULL FIRST. It is mandatory.**

## STEP 0 (MANDATORY — do this before writing any code)

Read `prompts/steelFramedCodeWriter.md` in full. Your COMPLETENESS checklist must cite which Steel-Framed rules you applied.

## Objective

Move the three streaming functions (`_stream_openai_events`, `_stream_minimax_events`, `_stream_anthropic_events`) from `agent/runtime.py` into the provider classes as `stream()` methods. After this phase, each provider has both `.call()` and `.stream()` methods. The dispatch site `_call_llm_streaming` is updated to use `get_provider(caller_key).stream(...)`.

**This is the final code extraction.** After B6, runtime.py should be ~2200 lines (from 2630).

## STEP 1: Discovery (mandatory per Steel-Framed Rule 1)

Read these files in full before writing any code:
1. `prompts/steelFramedCodeWriter.md` — your standing orders
2. `docs/specs/SPEC-RUNTIME-MODULAR-EXTRACTION-PHASE-1.md` §B.4.2 (_call_llm_streaming changes), §B.6 (StreamingCallKwargs + TestStreamingSignature), §B.5 (re-exports)
3. `agent/runtime.py` — find and read ALL THREE stream functions completely:
   - `def _stream_openai_events` (~line 265)
   - `def _stream_minimax_events` (~line 343)
   - `def _stream_anthropic_events` (~line 454)
   - Also read `_PROVIDER_STREAMERS` dict (~line 557)
   - Also read the `_call_llm_streaming` dispatch site (~line 2147-2161)
4. `agent/llm/openai_provider.py`, `agent/llm/minimax_provider.py`, `agent/llm/anthropic_provider.py` — the provider classes you're adding `stream()` to
5. `agent/llm/streaming.py` — the SSE helpers (sse_lines, parse_sse_line, etc.) the stream functions call
6. `agent/llm/cost.py` — `model_id` function
7. `agent/llm/convert.py` — Anthropic converters

## Deliverable — 5 edits

### Edit 1: Add `stream()` method to `agent/llm/openai_provider.py`

Move the body of `_stream_openai_events` VERBATIM into a `stream()` method on `OpenAIProvider`. The method signature is identical to `call()` — same 7 parameters.

**Internal reference updates inside the method body:**
- `_model_id(model)` → `model_id(model)` (from `agent.llm.cost`)
- `_urlopen_with_ssl_retry` → import from `agent.llm.streaming` (lazy import inside method, same pattern as `call()`)
- `_sse_lines` → `sse_lines` (from `agent.llm.streaming`)
- `_parse_sse_line` → `parse_sse_line`
- `_first_choice` → `first_choice`
- `_parse_sse_delta` → `parse_sse_delta`

**Add these imports to the top of openai_provider.py (if not already present):**
```python
from agent.llm.streaming import (
    sse_lines,
    parse_sse_line,
    parse_sse_delta,
    first_choice,
    urlopen_with_ssl_retry,
)
from agent.llm.cost import model_id
```

**Note:** The `call()` method currently uses a lazy import of `_urlopen_with_ssl_retry` from runtime. After B6, BOTH `call()` and `stream()` should import from `agent.llm.streaming` instead. Update the lazy import in `call()` too.

### Edit 2: Add `stream()` method to `agent/llm/minimax_provider.py`

Move the body of `_stream_minimax_events` VERBATIM. Same internal reference updates as Edit 1. Same imports.

### Edit 3: Add `stream()` method to `agent/llm/anthropic_provider.py`

Move the body of `_stream_anthropic_events` VERBATIM. Same updates plus:
- `_convert_messages_for_anthropic` → `convert_messages_for_anthropic` (from `agent.llm.convert`)
- `_convert_tools_for_anthropic` → `convert_tools_for_anthropic`

### Edit 4: Replace stream function defs + _PROVIDER_STREAMERS in `agent/runtime.py`

Find the three stream function definitions (~lines 265-556) and the `_PROVIDER_STREAMERS` dict (~line 557). Delete all four. Replace with:

```python
# ── Stream functions (moved to provider classes, Phase B6) ──────────────────
# Re-exported as bound methods for backward compatibility with test patches.
_stream_openai_events = OpenAIProvider("openai").stream
_stream_minimax_events = MiniMaxProvider().stream
_stream_anthropic_events = AnthropicProvider().stream

_PROVIDER_STREAMERS: dict[str, Any] = {
    "openai": _stream_openai_events,
    "minimax": _stream_minimax_events,
    "anthropic": _stream_anthropic_events,
    "openrouter": OpenAIProvider("openrouter").stream,
    "zai": OpenAIProvider("zai").stream,
}
```

**Key points:**
- Same bound-method re-export pattern as B4's `_call_openai`.
- `_PROVIDER_STREAMERS` dict is preserved for backward compat.
- The `OpenAIProvider`, `MiniMaxProvider`, `AnthropicProvider` are already imported at the top of runtime.py (from B4 re-exports).

### Edit 5: Update `_call_llm_streaming` dispatch in `agent/runtime.py`

Find the dispatch site (~line 2147):
```python
        streamer = _PROVIDER_STREAMERS.get(caller_key)
```

Replace the `streamer = _PROVIDER_STREAMERS.get(...)` + `for ev in _stream_with_ssl_retry(streamer, ...)` pattern with:

```python
        provider = _get_provider(caller_key)
        streamer = provider.stream
```

Then the existing `for ev in _stream_with_ssl_retry(streamer, ...)` call works unchanged — `streamer` is now the bound `provider.stream` method.

**IMPORTANT:** Do NOT change the `_call_llm_streaming` method signature. `StreamingCallKwargs` TypedDict and `TestStreamingSignature` must pass unchanged.

### Edit 6: Add streaming tests to `tests/test_llm_providers.py`

Add streaming tests to the existing `tests/test_llm_providers.py`:

**OpenAI stream tests:**
1. `test_openai_stream_yields_text_delta` — SSE text content forwarded
2. `test_openai_stream_yields_tool_call_delta` — SSE tool call fragments forwarded
3. `test_openai_stream_yields_done` — [DONE] → done event

**MiniMax stream tests:**
4. `test_minimax_stream_finish_reason_signals_done` — finish_reason="stop" → done

**Anthropic stream tests:**
5. `test_anthropic_stream_text_delta_forwarded` — text_delta events forwarded
6. `test_anthropic_stream_message_stop_signals_done` — message_stop → done event

**Use the mock pattern:** Mock `urllib.request.urlopen` to return a fake response object that yields SSE lines. This is the same pattern used in the existing `test_agent_runtime.py` streaming tests.

## Verification commands

```bash
# Provider classes have both call() and stream()
python3 -c "from agent.llm.openai_provider import OpenAIProvider; p = OpenAIProvider(); assert hasattr(p, 'call') and hasattr(p, 'stream'); print('OpenAI call+stream OK')"
python3 -c "from agent.llm.minimax_provider import MiniMaxProvider; p = MiniMaxProvider(); assert hasattr(p, 'call') and hasattr(p, 'stream'); print('MiniMax call+stream OK')"
python3 -c "from agent.llm.anthropic_provider import AnthropicProvider; p = AnthropicProvider(); assert hasattr(p, 'call') and hasattr(p, 'stream'); print('Anthropic call+stream OK')"

# runtime.py re-exports work
python3 -c "from agent.runtime import _stream_openai_events, _stream_minimax_events, _stream_anthropic_events, _PROVIDER_STREAMERS; print('re-export OK')"

# runtime.py compiles
python3 -c "from agent.runtime import AgentRuntime; print('runtime OK')"

# Old stream defs gone (must be 0)
grep -c "^def _stream_openai_events\|^def _stream_minimax_events\|^def _stream_anthropic_events" agent/runtime.py

# New stream tests pass
python3 -m pytest tests/test_llm_providers.py -v -k "stream"

# All B-phase tests pass (regression)
python3 -m pytest tests/test_llm_cost.py tests/test_llm_convert.py tests/test_llm_extractors.py tests/test_llm_providers.py tests/test_llm_registry.py tests/test_llm_streaming.py -q

# TestStreamingSignature passes unchanged (CRITICAL)
python3 -m pytest tests/test_agent_runtime.py::TestStreamingSignature -v

# runtime.py line count
wc -l agent/runtime.py

# No collateral damage
git diff --name-only agent/tools.py agent/enforcement.py tests/test_agent_runtime.py
```

## COMPLETENESS checklist

```
COMPLETENESS:
- [x/not done] STEP 0: Read prompts/steelFramedCodeWriter.md — cite 3 rules
- [x/not done] stream() added to OpenAIProvider — evidence: hasattr check
- [x/not done] stream() added to MiniMaxProvider — evidence: hasattr check
- [x/not done] stream() added to AnthropicProvider — evidence: hasattr check
- [x/not done] call() methods updated to import from agent.llm.streaming (not runtime) — evidence: grep
- [x/not done] runtime.py: 3 stream defs + _PROVIDER_STREAMERS replaced with re-exports — evidence: grep (0 old defs)
- [x/not done] _call_llm_streaming dispatch updated to use provider.stream — evidence: grep
- [x/not done] 6 new stream tests added to test_llm_providers.py — evidence: pytest
- [x/not done] All B-phase tests pass (regression) — evidence: pytest summary
- [x/not done] TestStreamingSignature passes unchanged — evidence: pytest
- [x/not done] runtime.py compiles + imports — evidence: python3 -c
- [x/not done] runtime.py line count dropped — evidence: wc -l
- [x/not done] No collateral damage — evidence: git diff
```

## Do NOT

- Do NOT change `_call_llm_streaming`'s method signature.
- Do NOT modify `StreamingCallKwargs` TypedDict.
- Do NOT modify `tests/test_agent_runtime.py`.
- Do NOT modify `agent/llm/streaming.py` (Phase B5 is done).
- Do NOT change the stream function logic — move verbatim (only: rename internal refs to public names).
- Do NOT remove the `_PROVIDER_STREAMERS` dict from runtime.py — re-export it.
