# Phase ARCH-1 + BACKLOG-1 Instructions — ARCHITECTURE.md Update + 5 Backlog Items

**Scope:** 2 tracks in one instructions file. Track ARCH-1: update ARCHITECTURE.md to reflect the new module structure. Track BACKLOG-1: fix 5 deferred items from the post-mortem.
**Rule reference:** `prompts/steelFramedCodeWriter.md` — **READ THIS FILE IN FULL FIRST. It is mandatory.**

## STEP 0 (MANDATORY)

Read `prompts/steelFramedCodeWriter.md` in full. Cite 3 rules in COMPLETENESS.

---

## TRACK ARCH-1: Update ARCHITECTURE.md

Read `docs/ARCHITECTURE.md` §2 (directory structure, around line 87), §3.21m (agent/runtime.py module responsibility, around line 1532), and §13 (file inventory, around line 3894). Update these sections to reflect the new modules.

### ARCH-1a: Update §2 directory structure (agent/ tree)

Find the `agent/` section in the directory tree (~line 87). The current tree shows the old structure. Update it to add the new modules. The tree should look like:

```
├── agent/                     # Local agent runtime — no UI dependencies
│   ├── __init__.py           # Exports: AgentRuntime, config classes, tool_middleware, llm package
│   ├── runtime.py           # AgentRuntime — tool loop, streaming, cost tracking (2344 lines, was 3297)
│   ├── tools.py              # Tool definitions + execution
│   ├── config.py             # LLM provider config + EnforcementConfig
│   ├── context.py            # System prompt builder + file context builder
│   ├── context_strategy.py   # Pluggable context compaction strategy
│   ├── tool_middleware.py    # Tool middleware chain (enforcement + stuck detection) — Phase A1
│   ├── enforcement.py        # Post-write verification: syntax guard, test runner, lint check
│   ├── special_agents.py     # Coder + Debugger + Crabcakes agent definitions
│   ├── kb_lookup.py          # KB cosine-sim retrieval
│   ├── kb_server.py          # KB HTTP server
│   ├── llm/                  # LLM provider abstraction package — Phase B1-B6
│   │   ├── __init__.py       # Public API: get_provider, list_providers, LLMProvider, LLMResponse
│   │   ├── protocol.py       # LLMProvider Protocol + LLMResponse dataclass
│   │   ├── registry.py       # Provider registry: get_provider(id) -> LLMProvider
│   │   ├── openai_provider.py # OpenAIProvider (handles openai, openrouter, zai)
│   │   ├── minimax_provider.py # MiniMaxProvider
│   │   ├── anthropic_provider.py # AnthropicProvider
│   │   ├── streaming.py      # SSE helpers + SSL retry infrastructure
│   │   ├── extractors.py     # Response extractors (tool_calls, text_content, usage)
│   │   ├── convert.py        # Anthropic message/tool format converters
│   │   └── cost.py           # Cost tables + model_id + cost_for_model
```

### ARCH-1b: Add new §3 sections for the new modules

After §3.21m (the current `agent/runtime.py` section), add these new subsections:

**§3.21m.1 `agent/tool_middleware.py` — Tool Middleware Chain**
- Responsibility: Composable middleware that wraps tool execution with cross-cutting concerns (enforcement, stuck detection).
- Public API: `ToolMiddleware` (Protocol), `ToolContext` (dataclass), `EnforcementMiddleware`, `StuckDetectionMiddleware`, `ToolMiddlewareChain`.
- Architecture: `agent/` layer — imports only from `agent.tools` and stdlib. No UI/gateway/runtime imports.

**§3.21m.2 `agent/llm/` — LLM Provider Abstraction Package**
- Responsibility: Encapsulate LLM provider wire protocols behind a uniform interface.
- Public API: `get_provider(id)`, `list_providers()`, `LLMProvider` Protocol, `LLMResponse`.
- Sub-modules: protocol, registry, openai_provider, minimax_provider, anthropic_provider, streaming, extractors, convert, cost.
- Architecture: `agent/` layer — imports from stdlib + `agent.tools` only. No UI/gateway imports.

### ARCH-1c: Update §3.21m (agent/runtime.py)

Update the `agent/runtime.py` description to note:
- Line count reduced from 3297 → 2344 (28.9% reduction) via modular extraction.
- Cost functions, LLM callers, stream functions, SSE helpers, extractors, and converters have been extracted to `agent/llm/` and `agent/tool_middleware.py`.
- Re-exported under legacy underscore names for backward compatibility.
- Tool execution now routes through `self._tool_chain.run()` (ToolMiddlewareChain).
- LLM dispatch now uses `get_provider(caller_key).call()` and `.stream()`.

### ARCH-1d: Update §13 file inventory

Update the `agent/` section in the file inventory (~line 3894) to show:
- `runtime.py` line count: ~2344 (was ~2418)
- Add `tool_middleware.py`
- Add `llm/` subdirectory with all 10 modules

---

## TRACK BACKLOG-1: Fix 5 Deferred Items

### BACKLOG-1a: Switch extractors to `response_format: str` parameter

**File:** `agent/llm/extractors.py`, `agent/runtime.py`

Currently the extractors look up `_RESPONSE_FORMAT` via a lazy import from runtime. Switch them to take an explicit `response_format: str` parameter instead.

1. In `agent/llm/extractors.py`:
   - Change `extract_tool_calls(response, provider)` → `extract_tool_calls(response, response_format="openai")`
   - Change `extract_text_content(response, provider)` → `extract_text_content(response, response_format="openai")`
   - Change `extract_usage(response, provider="openai")` → `extract_usage(response, response_format="openai")`
   - Replace `_get_response_format().get(provider, "openai")` with the `response_format` parameter directly.
   - Remove the `_get_response_format()` function entirely.

2. In `agent/runtime.py`, update all call sites (use `grep -n "_extract_tool_calls\|_extract_text_content\|_extract_usage" agent/runtime.py` to find them):
   - Before each call, resolve the format: `fmt = get_provider(loop_provider).response_format` (or use `_RESPONSE_FORMAT.get(provider, "openai")` since `_RESPONSE_FORMAT` still exists).
   - Pass `response_format=fmt` to each call.

3. Update `tests/test_llm_extractors.py`:
   - Change all `@patch("agent.llm.extractors._get_response_format")` decorators to pass `response_format="openai"` or `response_format="anthropic"` directly.

### BACKLOG-1b: Fix TestApproval/TestToolLoop 3-arg lambda

**File:** `tests/test_agent_runtime.py`

Find all 3-arg `_on_tool_call_result` lambdas and update to 4-arg:
- `lambda sk2, n, r: results.append((n, r))` → `lambda sk2, n, r, success: results.append((n, r))`
- Use `grep -n "_on_tool_call_result.*lambda" tests/test_agent_runtime.py` to find all occurrences.

### BACKLOG-1c: Fix TestStreaming delta count

**File:** `tests/test_agent_runtime.py`

Find `test_text_delta_fires_incrementally`. Update the assertion from 3 deltas to 4 (the BUG #21 turn-start empty delta is intentional):
- `assert len(deltas) == 3` → `assert len(deltas) == 4`
- Add `assert deltas[0] == ""` to document the turn-start signal.

### BACKLOG-1d: Add streaming dispatch integration test

**File:** `tests/test_llm_providers.py`

Add a test that mocks `get_provider` and asserts `.stream()` is called from the dispatch path. Pattern: construct a minimal mock that patches `_get_provider`, calls `_call_llm_streaming`, and verifies the mock provider's `.stream` was invoked.

This is a light test — it doesn't need to construct a full `AgentRuntime`. It tests the dispatch wiring, not the streaming protocol.

### BACKLOG-1e: Deprecate _PROVIDER_CALLERS / _PROVIDER_STREAMERS dicts

**File:** `agent/runtime.py`

Add deprecation comments to the two dicts:
```python
_PROVIDER_CALLERS: dict[str, Any] = {
    # DEPRECATED: dispatch now uses get_provider(caller_key).call().
    # This dict is retained for backward compatibility with test patches.
    # Do not add new dispatch logic here — use agent.llm.registry.get_provider().
    ...
}
```

Same for `_PROVIDER_STREAMERS`.

---

## Verification commands

```bash
# ARCHITECTURE.md updated
grep -c "tool_middleware.py" docs/ARCHITECTURE.md  # must be >= 2 (§2 tree + §13 inventory)
grep -c "agent/llm/" docs/ARCHITECTURE.md  # must be >= 3 (§2 tree + §3 section + §13 inventory)
grep -c "2344" docs/ARCHITECTURE.md  # must be >= 1 (updated line count)

# Extractors switched to response_format parameter
grep -c "_get_response_format" agent/llm/extractors.py  # must be 0 (removed)
grep -c "response_format" agent/llm/extractors.py  # must be >= 3 (3 functions)

# Test fixes
python3 -m pytest tests/test_agent_runtime.py::TestApproval -o addopts="" -q -p no:cacheprovider
python3 -m pytest tests/test_agent_runtime.py::TestStreaming::test_text_delta_fires_incrementally -o addopts="" -q -p no:cacheprovider

# All B-phase tests still pass
python3 -m pytest tests/test_llm_cost.py tests/test_llm_convert.py tests/test_llm_extractors.py tests/test_llm_providers.py tests/test_llm_registry.py tests/test_llm_streaming.py tests/test_tool_middleware.py -o addopts="" -q

# runtime compiles
python3 -c "from agent.runtime import AgentRuntime; print('OK')"
```

## COMPLETENESS checklist

```
COMPLETENESS:
- [x/not done] STEP 0: Read steelFramedCodeWriter.md — cite 3 rules
- [x/not done] ARCH-1a: §2 directory tree updated with new modules
- [x/not done] ARCH-1b: §3 sections added for tool_middleware.py + agent/llm/
- [x/not done] ARCH-1c: §3.21m updated (line count, extraction note)
- [x/not done] ARCH-1d: §13 file inventory updated
- [x/not done] BACKLOG-1a: extractors switched to response_format parameter
- [x/not done] BACKLOG-1b: TestApproval/TestToolLoop lambdas fixed (4-arg)
- [x/not done] BACKLOG-1c: TestStreaming delta count fixed
- [x/not done] BACKLOG-1d: Streaming dispatch integration test added
- [x/not done] BACKLOG-1e: Deprecation comments on dicts
- [x/not done] All tests pass
```
