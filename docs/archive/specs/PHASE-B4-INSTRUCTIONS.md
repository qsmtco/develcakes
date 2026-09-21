# Phase B4 Instructions — Create Provider Registry + Protocol + 3 Provider Classes

**Track:** B Phase 2a (non-streaming extraction — FINAL phase of 2a)
**Scope:** Create 6 NEW files in `agent/llm/`, edit `agent/runtime.py` (re-export block + dispatch update), create 2 NEW test files.
**Spec reference:** `docs/specs/SPEC-RUNTIME-MODULAR-EXTRACTION-PHASE-1.md` §B.3.1, §B.3.2, §B.3.3, §B.3.4, §B.3.9, §B.3.10, §B.4.1, §B.5
**Rule reference:** `prompts/steelFramedCodeWriter.md` — **READ THIS FILE IN FULL FIRST. It is mandatory.**

## STEP 0 (MANDATORY — do this before writing any code)

Read `prompts/steelFramedCodeWriter.md` in full. Your COMPLETENESS checklist must cite which Steel-Framed rules you applied.

## Objective

Extract the three non-streaming LLM caller functions (`_call_openai`, `_call_minimax`, `_call_anthropic`) from `agent/runtime.py` into provider classes in a new `agent/llm/` package. Create a `LLMProvider` Protocol, a provider registry, and wire the registry into `_call_llm`.

**This phase does NOT touch streaming.** The stream functions (`_stream_openai_events`, etc.) stay in runtime.py for now — they move in Phase B6. Only the non-streaming `call()` methods move in this phase.

## STEP 1: Discovery (mandatory per Steel-Framed Rule 1)

Read these files in full before writing any code:
1. `prompts/steelFramedCodeWriter.md` — your standing orders
2. `docs/specs/SPEC-RUNTIME-MODULAR-EXTRACTION-PHASE-1.md` §B.3.1 (protocol), §B.3.2 (OpenAI), §B.3.3 (MiniMax), §B.3.4 (Anthropic), §B.3.9 (registry), §B.3.10 (__init__), §B.4.1 (_call_llm changes), §B.5 (re-exports)
3. `agent/runtime.py` — find and read ALL THREE caller functions completely:
   - `def _call_openai` (~line 180) — handles OpenAI, OpenRouter, ZAI
   - `def _call_minimax` (~line 237) — MiniMax-specific endpoint + body-level error check
   - `def _call_anthropic` (~line 285) — Anthropic-specific headers + system message extraction + converters
   - Also read `_PROVIDER_CALLERS` dict (~line 339), `get_valid_callers()` (~line 348), `_RESPONSE_FORMAT` (~line 374-378)
4. `agent/llm/cost.py` — the `model_id` function (the providers call it)
5. `agent/llm/convert.py` — the Anthropic converters (AnthropicProvider calls them)

## Deliverable — 6 NEW files + 1 edit to runtime.py + 2 test files

### File 1: `agent/llm/protocol.py` (NEW)

Per spec §B.3.1. Defines the `LLMProvider` Protocol and `LLMResponse` dataclass:

```python
"""LLM provider protocol and response dataclass."""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Iterator, Protocol


@dataclass
class LLMResponse:
    """Normalized response from any LLM provider."""
    text: str = ""
    tool_calls: list[tuple[str, str, dict]] = field(default_factory=list)
    usage: tuple[int, int] = (0, 0)
    raw: dict = field(default_factory=dict)


class LLMProvider(Protocol):
    """One class per provider wire protocol."""

    @property
    def provider_id(self) -> str: ...

    @property
    def response_format(self) -> str: ...

    def call(
        self,
        base_url: str,
        api_key: str,
        model: str,
        messages: list[dict],
        tools: list[dict] | None,
        timeout: float,
        x_title: str = "",
    ) -> dict: ...
```

### File 2: `agent/llm/openai_provider.py` (NEW)

Per spec §B.3.2. Wraps the `_call_openai` function body as a method. Handles openai, openrouter, zai.

```python
"""OpenAI-compatible LLM provider (openai, openrouter, zai)."""

from __future__ import annotations
import json
import urllib.error
import urllib.request

from agent.llm.cost import model_id
from agent.runtime import _urlopen_with_ssl_retry


class OpenAIProvider:
    """Handles OpenAI, OpenRouter, and ZAI APIs (all OpenAI-compatible).

    The provider_id is set at construction so a single class serves multiple
    registry entries. The wire protocol is identical; only credentials and
    base_url differ (both passed by the caller).
    """

    def __init__(self, provider_id: str = "openai"):
        self._id = provider_id

    @property
    def provider_id(self) -> str:
        return self._id

    @property
    def response_format(self) -> str:
        return "openai"

    def call(self, base_url, api_key, model, messages, tools, timeout, x_title=""):
        # Body moved VERBATIM from _call_openai (runtime.py ~line 180)
        # The function body uses: model_id(model), _urlopen_with_ssl_retry, json
        ...
```

**IMPORTANT — the `_urlopen_with_ssl_retry` reference:** This function is still in `runtime.py` (it moves to `agent/llm/streaming.py` in Phase B5). For now, the provider imports it from runtime: `from agent.runtime import _urlopen_with_ssl_retry`. This is a lazy import done at module level — it works because runtime.py is fully loaded before any provider is instantiated.

**CRITICAL:** Move the `_call_openai` function body **VERBATIM** into the `call` method. Replace the `_model_id(model)` call with `model_id(model)` (the public name from `agent.llm.cost`). Keep all other references (`_urlopen_with_ssl_retry`, `json`, `urllib`) as-is.

### File 3: `agent/llm/minimax_provider.py` (NEW)

Per spec §B.3.3. Wraps the `_call_minimax` function body.

```python
"""MiniMax ChatCompletion v2 LLM provider."""

from __future__ import annotations
import json
import urllib.error
import urllib.request

from agent.llm.cost import model_id
from agent.runtime import _urlopen_with_ssl_retry


class MiniMaxProvider:
    """MiniMax ChatCompletion v2 API.

    Uses the OpenAI-compatible message format but has a different endpoint
    path (/text/chatcompletion_v2), body-level error envelopes, and a different
    finish-detection mechanism.
    """

    @property
    def provider_id(self) -> str:
        return "minimax"

    @property
    def response_format(self) -> str:
        return "openai"  # response shape is OpenAI-compatible

    def call(self, base_url, api_key, model, messages, tools, timeout, x_title=""):
        # Body moved VERBATIM from _call_minimax (runtime.py ~line 237)
        ...
```

### File 4: `agent/llm/anthropic_provider.py` (NEW)

Per spec §B.3.4. Wraps the `_call_anthropic` function body. Calls the Anthropic converters.

```python
"""Anthropic Messages API LLM provider."""

from __future__ import annotations
import json
import urllib.error
import urllib.request
from typing import Any

from agent.llm.cost import model_id
from agent.llm.convert import convert_messages_for_anthropic, convert_tools_for_anthropic
from agent.runtime import _urlopen_with_ssl_retry


class AnthropicProvider:
    """Anthropic Messages API.

    Requires message/tool format conversion (system message extraction,
    content-block format). Uses x-api-key header, not Bearer auth.
    """

    @property
    def provider_id(self) -> str:
        return "anthropic"

    @property
    def response_format(self) -> str:
        return "anthropic"

    def call(self, base_url, api_key, model, messages, tools, timeout, x_title=""):
        # Body moved VERBATIM from _call_anthropic (runtime.py ~line 285)
        # Replace _convert_messages_for_anthropic → convert_messages_for_anthropic
        # Replace _convert_tools_for_anthropic → convert_tools_for_anthropic
        # Replace _model_id → model_id
        ...
```

### File 5: `agent/llm/registry.py` (NEW)

Per spec §B.3.9. Maps provider IDs to provider instances.

```python
"""LLM provider registry."""

from __future__ import annotations

from agent.llm.openai_provider import OpenAIProvider
from agent.llm.minimax_provider import MiniMaxProvider
from agent.llm.anthropic_provider import AnthropicProvider

_REGISTRY: dict[str, object] = {
    "openai": OpenAIProvider("openai"),
    "openrouter": OpenAIProvider("openrouter"),
    "zai": OpenAIProvider("zai"),
    "minimax": MiniMaxProvider(),
    "anthropic": AnthropicProvider(),
}


def get_provider(provider_id: str):
    """Return the LLMProvider instance for the given provider ID.

    Raises KeyError if the provider is not registered.
    """
    if provider_id not in _REGISTRY:
        raise KeyError(
            f"Unknown LLM provider: {provider_id!r}. "
            f"Registered: {sorted(_REGISTRY.keys())}"
        )
    return _REGISTRY[provider_id]


def list_providers() -> list[str]:
    """Return sorted list of registered provider IDs."""
    return sorted(_REGISTRY.keys())
```

### File 6: Update `agent/llm/__init__.py`

Replace the minimal stub with the public API per spec §B.3.10:

```python
"""LLM provider abstraction layer.

Public API:
    get_provider(id) -> LLMProvider
    list_providers() -> list[str]
    LLMProvider — Protocol
    LLMResponse — normalized response dataclass
"""
from agent.llm.protocol import LLMProvider, LLMResponse
from agent.llm.registry import get_provider, list_providers

__all__ = [
    "get_provider",
    "list_providers",
    "LLMProvider",
    "LLMResponse",
]
```

**NOTE:** Do NOT export `SSEEvent` from `__init__.py` yet — that's Phase B5 (streaming). Only export `get_provider`, `list_providers`, `LLMProvider`, `LLMResponse`.

### File 7: Edit `agent/runtime.py` — re-exports + dispatch update

**Edit 7a: Replace the 3 caller function defs + _PROVIDER_CALLERS dict with re-exports**

Find `_call_openai`, `_call_minimax`, `_call_anthropic`, and the `_PROVIDER_CALLERS` dict. Delete all three function definitions and the dict. Replace with:

```python
# ── LLM providers (extracted to agent/llm/, Phase B4) ───────────────────────
# Re-exported under legacy names for backward compatibility.
from agent.llm.openai_provider import OpenAIProvider
from agent.llm.minimax_provider import MiniMaxProvider
from agent.llm.anthropic_provider import AnthropicProvider
from agent.llm.registry import get_provider as _get_provider

# Bound methods for test-patch compatibility (patch("agent.runtime._call_openai"))
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

**IMPORTANT:** `get_valid_callers()` (which reads `_PROVIDER_CALLERS.keys()`) still works — the dict is re-exported. Do NOT modify `get_valid_callers()`.

**IMPORTANT:** The `_RESPONSE_FORMAT` population loop (line ~377) does `if _caller is _call_anthropic`. After the re-export, `_call_anthropic` is a bound method of `AnthropicProvider`. The identity check `_caller is _call_anthropic` still works because `_PROVIDER_CALLERS["anthropic"]` IS `_call_anthropic` (same bound method). **Do NOT modify the _RESPONSE_FORMAT loop.**

### File 8: `tests/test_llm_providers.py` (NEW)

Test the provider classes. Spec §B.9 cases 1-4, 11-12, 16-18.

**OpenAIProvider tests:**
1. `test_openai_call_builds_correct_request` — verify endpoint, headers, payload (mock urllib)
2. `test_openai_call_includes_tools_when_provided` — tool_choice=auto set
3. `test_openai_call_omits_tools_when_none` — no tools key
4. `test_openai_call_raises_on_http_error` — HTTPError → RuntimeError
5. `test_openai_provider_id_and_response_format` — properties correct

**MiniMaxProvider tests:**
6. `test_minimax_detects_body_level_error` — HTTP 200 with base_resp.status_code != 0 → RuntimeError
7. `test_minimax_call_success` — normal response returned
8. `test_minimax_provider_id_and_response_format` — properties correct

**AnthropicProvider tests:**
9. `test_anthropic_extracts_system_message` — system message moved to payload["system"]
10. `test_anthropic_strips_duplicate_system` — only first system message extracted
11. `test_anthropic_converts_tools` — tools use input_schema format
12. `test_anthropic_provider_id_and_response_format` — properties correct

**Sad-path:**
13. `test_openai_call_with_x_title_sets_headers` — X-Title and HTTP-Referer headers

### File 9: `tests/test_llm_registry.py` (NEW)

Spec §B.9 cases 36-40.

1. `test_get_provider_openai` — returns OpenAIProvider
2. `test_get_provider_minimax` — returns MiniMaxProvider
3. `test_get_provider_anthropic` — returns AnthropicProvider
4. `test_get_provider_openrouter` — returns OpenAIProvider (alias)
5. `test_get_provider_unknown_raises_keyerror` — clear error message
6. `test_list_providers_sorted` — returns sorted list

## Verification commands

```bash
# New package imports
python3 -c "from agent.llm import get_provider, list_providers, LLMProvider, LLMResponse; print('package import OK')"

# Provider classes import
python3 -c "from agent.llm.openai_provider import OpenAIProvider; from agent.llm.minimax_provider import MiniMaxProvider; from agent.llm.anthropic_provider import AnthropicProvider; print('providers import OK')"

# runtime.py re-exports work
python3 -c "from agent.runtime import _call_openai, _call_minimax, _call_anthropic, _PROVIDER_CALLERS; print('re-export OK')"

# runtime.py compiles
python3 -c "from agent.runtime import AgentRuntime; print('runtime OK')"

# New provider tests pass
python3 -m pytest tests/test_llm_providers.py -v

# New registry tests pass
python3 -m pytest tests/test_llm_registry.py -v

# Existing runtime tests pass (CRITICAL — no regression)
python3 -m pytest tests/test_agent_runtime.py -o addopts="" -q -k "CostComputation or extract or anthropic or Approval or Stuck" 2>&1 | tail -5

# Old caller defs gone from runtime.py
grep -c "^def _call_openai\|^def _call_minimax\|^def _call_anthropic" agent/runtime.py  # must be 0

# runtime.py line count
wc -l agent/runtime.py

# No collateral damage
git diff --name-only agent/tools.py agent/enforcement.py tests/test_agent_runtime.py  # must be empty
```

## COMPLETENESS checklist

```
COMPLETENESS:
- [x/not done] STEP 0: Read prompts/steelFramedCodeWriter.md — cite 3 rules
- [x/not done] agent/llm/protocol.py created (LLMProvider Protocol + LLMResponse dataclass)
- [x/not done] agent/llm/openai_provider.py created (OpenAIProvider with call method, verbatim body)
- [x/not done] agent/llm/minimax_provider.py created (MiniMaxProvider with call method, verbatim body)
- [x/not done] agent/llm/anthropic_provider.py created (AnthropicProvider with call method, verbatim body)
- [x/not done] agent/llm/registry.py created (get_provider, list_providers)
- [x/not done] agent/llm/__init__.py updated with public API exports
- [x/not done] agent/runtime.py: 3 caller defs + _PROVIDER_CALLERS replaced with re-exports
- [x/not done] tests/test_llm_providers.py created (13 tests)
- [x/not done] tests/test_llm_registry.py created (6 tests)
- [x/not done] All new tests pass
- [x/not done] Existing runtime tests pass (no regression)
- [x/not done] runtime.py compiles + imports
- [x/not done] runtime.py line count dropped
- [x/not done] No collateral damage
```

## Do NOT

- Do NOT touch streaming functions (`_stream_openai_events`, `_stream_minimax_events`, `_stream_anthropic_events`). They stay in runtime.py.
- Do NOT modify `_PROVIDER_STREAMERS` dict — it stays in runtime.py (Phase B6).
- Do NOT modify `get_valid_callers()` — it reads `_PROVIDER_CALLERS.keys()` which still works.
- Do NOT modify the `_RESPONSE_FORMAT` population loop.
- Do NOT modify `tests/test_agent_runtime.py`.
- Do NOT change the call logic — move verbatim into methods.
