# Phase 4 — Remove provider alias debt + migrate consumers

**Spec:** `docs/specs/SPEC-RUNTIME-TERMINAL-PATH-CONSOLIDATION.md` §2.3 Edits I, J, K, L, M, N, O, P
**Scope:** `agent/runtime.py` + `tests/test_agent_runtime.py` + `scripts/audit_streaming_scenarios.py` + `scripts/audit_attack_scenarios.py` + `agent/llm/streaming.py` + `utils/provider_test.py`

## Goal

Remove the provider alias debt (`_call_openai`, `_call_minimax`, `_call_anthropic`, `_stream_openai_events`, `_stream_minimax_events`, `_stream_anthropic_events`, `_PROVIDER_STREAMERS`) and migrate ALL consumers to use provider classes directly.

## Required reading first

Read these IN FULL before writing any code:
- `agent/runtime.py` lines 82-190 (the alias definitions + `_PROVIDER_CALLERS` + `_RESPONSE_FORMAT` + `_PROVIDER_STREAMERS`)
- `agent/llm/registry.py` (the `get_provider` function)
- `agent/llm/openai_provider.py`, `agent/llm/minimax_provider.py`, `agent/llm/anthropic_provider.py` (the provider classes)
- `docs/specs/SPEC-RUNTIME-TERMINAL-PATH-CONSOLIDATION.md` §2.3

## Consumer sites (verified by grep — MUST all be migrated)

1. `tests/test_agent_runtime.py` — 9 test methods import `_stream_openai_events`, `_stream_anthropic_events`, `_stream_minimax_events`, `_call_minimax`, `_call_anthropic`. Grep: `grep -n "from agent.runtime import.*_stream_\|from agent.runtime import.*_call_" tests/test_agent_runtime.py`
2. `scripts/audit_streaming_scenarios.py` — 9 `patch("agent.runtime._PROVIDER_STREAMERS", ...)` sites
3. `scripts/audit_attack_scenarios.py` — imports `_PROVIDER_STREAMERS` + `_PROVIDER_CALLERS`; uses them in 5 places
4. `agent/llm/streaming.py:77,370-371` — 3 docstring references
5. `utils/provider_test.py:96` — 1 docstring reference
6. `tests/test_llm_providers.py:735` — 1 docstring reference

## Edits to `agent/runtime.py`

### Edit I — Delete `_call_*` aliases, migrate `_PROVIDER_CALLERS`

Find the block (around lines 102-118):
```python
_call_openai = OpenAIProvider("openai").call
_call_minimax = MiniMaxProvider().call
_call_anthropic = AnthropicProvider().call

_PROVIDER_CALLERS: dict[str, Any] = {
    "openai": _call_openai,
    "minimax": _call_minimax,
    "anthropic": _call_anthropic,
    "openrouter": OpenAIProvider("openrouter").call,
    "zai": OpenAIProvider("zai").call,
}
```

Replace with (delete the 3 aliases; inline the provider calls into the dict):
```python
# Provider dispatch is via agent.llm.registry.get_provider() (Phase B4).
# _call_llm uses _get_provider(caller_key).call(...).
# _call_llm_streaming uses _get_provider(caller_key).stream(...).
# The _call_openai / _call_minimax / _call_anthropic bound-method aliases
# (preserved for test-patch compat in Phase B4) are removed in
# SPEC-RUNTIME-TERMINAL-PATH-CONSOLIDATION §2.3.
_PROVIDER_CALLERS: dict[str, Any] = {
    "openai": OpenAIProvider("openai").call,
    "minimax": MiniMaxProvider().call,
    "anthropic": AnthropicProvider().call,
    "openrouter": OpenAIProvider("openrouter").call,
    "zai": OpenAIProvider("zai").call,
}
```

### Edit K — Fix `_RESPONSE_FORMAT` (BUG #1: identity comparison with deleted alias)

Find the block (around lines 145-152):
```python
_RESPONSE_FORMAT: dict[str, str] = {}
for _pk, _caller in _PROVIDER_CALLERS.items():
    if _caller is _call_anthropic:
        _RESPONSE_FORMAT[_pk] = "anthropic"
    else:
        _RESPONSE_FORMAT[_pk] = "openai"
```

Replace with (key off caller key string, not identity comparison):
```python
# Response format families — derived from caller key (BUG #1 fix: was
# identity comparison against deleted _call_anthropic alias).
# Any provider not in {"anthropic"} uses OpenAI-format responses.
_RESPONSE_FORMAT: dict[str, str] = {
    pk: ("anthropic" if pk == "anthropic" else "openai")
    for pk in _PROVIDER_CALLERS
}
```

### Edit J — Delete `_stream_*_events` aliases and `_PROVIDER_STREAMERS`

Find the block (around lines 179-189):
```python
_stream_openai_events = OpenAIProvider("openai").stream
_stream_minimax_events = MiniMaxProvider().stream
_stream_anthropic_events = AnthropicProvider().stream

_PROVIDER_STREAMERS: dict[str, Any] = {
    "openai": _stream_openai_events,
    ...
}
```

Delete the ENTIRE block (both the 3 aliases AND the `_PROVIDER_STREAMERS` dict). Replace with a comment:
```python
# _PROVIDER_STREAMERS and the _stream_*_events aliases were dispatch
# infrastructure superseded by _get_provider(caller_key).stream in Phase B6.
# Removed in SPEC-RUNTIME-TERMINAL-PATH-CONSOLIDATION §2.3.
```

### Edit K-2 — Update `__all__`

Remove `"_PROVIDER_STREAMERS"` from `__all__` (keep `"_PROVIDER_CALLERS"`). Add `"TurnStatus"` and `"TurnResult"` (if not already added in Phase 2a — verify first).

## Edits to test/script consumers

### `tests/test_agent_runtime.py` — migrate 9 import sites

For each test that does `from agent.runtime import _stream_openai_events` (or `_stream_anthropic_events`, `_stream_minimax_events`, `_call_minimax`, `_call_anthropic`):

- Replace `from agent.runtime import _stream_openai_events` → `from agent.llm.openai_provider import OpenAIProvider`
- Replace `_stream_openai_events(...)` calls → `OpenAIProvider("openai").stream(...)`
- Replace `from agent.runtime import _stream_anthropic_events` → `from agent.llm.anthropic_provider import AnthropicProvider`
- Replace `_stream_anthropic_events(...)` → `AnthropicProvider().stream(...)`
- Replace `from agent.runtime import _stream_minimax_events` → `from agent.llm.minimax_provider import MiniMaxProvider`
- Replace `_stream_minimax_events(...)` → `MiniMaxProvider().stream(...)`
- Replace `from agent.runtime import _call_minimax` → `from agent.llm.minimax_provider import MiniMaxProvider`
- Replace `_call_minimax(...)` → `MiniMaxProvider().call(...)`
- Replace `from agent.runtime import _call_anthropic` → `from agent.llm.anthropic_provider import AnthropicProvider`
- Replace `_call_anthropic(...)` → `AnthropicProvider().call(...)`

Grep to find ALL sites: `grep -n "from agent.runtime import.*_stream_\|from agent.runtime import.*_call_\|_stream_openai_events\|_stream_anthropic_events\|_stream_minimax_events\|_call_minimax\|_call_anthropic" tests/test_agent_runtime.py`

### `scripts/audit_streaming_scenarios.py` — migrate 9 patch sites

Replace each `with patch("agent.runtime._PROVIDER_STREAMERS", {"openai": streamer}):` with:
```python
with patch.object(OpenAIProvider, "stream", streamer):
```
Add `from agent.llm.openai_provider import OpenAIProvider` at the top.

### `scripts/audit_attack_scenarios.py` — migrate imports + usage

Replace `from agent.runtime import AgentRuntime, _PROVIDER_CALLERS, _PROVIDER_STREAMERS` with `from agent.runtime import AgentRuntime, _PROVIDER_CALLERS`. Replace `_PROVIDER_STREAMERS` usage (the `.get("")` smoke tests and `result in _PROVIDER_STREAMERS` checks) with direct provider checks or remove them.

### `agent/llm/streaming.py` — update 3 docstring references

Replace `_stream_openai_events` / `_stream_minimax_events` / `_stream_anthropic_events` in docstrings with `OpenAIProvider("openai").stream` etc.

### `utils/provider_test.py` — update 1 docstring reference

Replace `_call_minimax` in the docstring with `MiniMaxProvider().call`.

### `tests/test_llm_providers.py` — update 1 docstring reference

Replace `_PROVIDER_STREAMERS` in the docstring (line ~735) with a reference to `get_provider(caller_key).stream`.

## Verification commands (run all, paste output)

```bash
# 1. Compiles
python3 -m py_compile agent/runtime.py && echo COMPILE_OK

# 2. Aliases removed from agent.runtime
python3 -c "
import agent.runtime
for name in ('_call_openai', '_call_minimax', '_call_anthropic',
             '_stream_openai_events', '_stream_minimax_events',
             '_stream_anthropic_events', '_PROVIDER_STREAMERS'):
    assert not hasattr(agent.runtime, name), f'{name} still exists!'
print('all aliases removed OK')
"

# 3. _PROVIDER_CALLERS still works (values are direct provider calls)
python3 -c "from agent.runtime import _PROVIDER_CALLERS, get_valid_callers; print(sorted(get_valid_callers())); print(len(_PROVIDER_CALLERS))"

# 4. _RESPONSE_FORMAT derived correctly (no NameError)
python3 -c "from agent.runtime import _RESPONSE_FORMAT; print(_RESPONSE_FORMAT)"

# 5. Grep sweep — NO remaining references to removed symbols (except local collision)
grep -rn "_stream_openai_events\|_stream_minimax_events\|_stream_anthropic_events\|_PROVIDER_STREAMERS" --include="*.py" . | grep -v "tests/generate_synthetic_conversations.py\|_call_minimax"
# Expected: 0 matches

# 6. Grep for _call_openai/_call_minimax/_call_anthropic (except local collision)
grep -rn "_call_openai\|_call_anthropic" --include="*.py" . | grep -v "tests/generate_synthetic_conversations.py\|_call_minimax\|agent/runtime.py"
# Expected: 0 matches (agent/runtime.py has the removal comment; generate_synthetic has a local _call_minimax)

# 7. Full test suite
XDG_CONFIG_HOME=/tmp/cctest_home/.config timeout 120 python3 -m pytest tests/test_agent_runtime.py -q --no-header --timeout=15 2>&1 | tail -5
# Expected: same 6 pre-existing failures (4 GTK/drawer + 2 approval hangs), no NEW failures
```

## COMPLETENESS checklist (mandatory)

```
COMPLETENESS:
- [x/not done] Edit I: _call_* aliases deleted, _PROVIDER_CALLERS migrated — evidence: hasattr check
- [x/not done] Edit K: _RESPONSE_FORMAT uses pk=="anthropic" (no identity comparison) — evidence: _RESPONSE_FORMAT output
- [x/not done] Edit J: _stream_*_events + _PROVIDER_STREAMERS deleted — evidence: hasattr check
- [x/not done] Edit K-2: __all__ updated (_PROVIDER_STREAMERS removed) — evidence: grep
- [x/not done] tests/test_agent_runtime.py: 9 import sites migrated — evidence: grep step 5/6 = 0
- [x/not done] scripts/audit_streaming_scenarios.py: 9 patch sites migrated — evidence: grep
- [x/not done] scripts/audit_attack_scenarios.py: imports + usage migrated — evidence: grep
- [x/not done] agent/llm/streaming.py: 3 docstring refs updated — evidence: grep
- [x/not done] utils/provider_test.py: 1 docstring ref updated — evidence: grep
- [x/not done] tests/test_llm_providers.py: 1 docstring ref updated — evidence: grep
- [x/not done] py_compile OK — evidence: step 1
- [x/not done] Grep sweeps: 0 remaining references — evidence: steps 5/6
- [x/not done] Test suite: no new failures — evidence: step 7
```
