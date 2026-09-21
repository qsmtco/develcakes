# Phase B3 Instructions — Extract Response Extractors to agent/llm/extractors.py

**Track:** B Phase 2a (non-streaming extraction)
**Scope:** Create `agent/llm/extractors.py` (NEW), edit `agent/runtime.py` (re-export block), create `tests/test_llm_extractors.py` (NEW).
**Spec reference:** `docs/specs/SPEC-RUNTIME-MODULAR-EXTRACTION-PHASE-1.md` §B.3.6, §B.5
**Rule reference:** `prompts/steelFramedCodeWriter.md` — **READ THIS FILE IN FULL FIRST. It is mandatory.**

## STEP 0 (MANDATORY — do this before writing any code)

Read `prompts/steelFramedCodeWriter.md` in full. Your COMPLETENESS checklist must cite which Steel-Framed rules you applied.

## Objective

Extract the three response-extractor functions from `agent/runtime.py` into a new `agent/llm/extractors.py` module. These parse LLM API responses (tool calls, text content, usage tokens) for both OpenAI and Anthropic formats.

**Key design decision for this phase:** The extractors currently look up `_RESPONSE_FORMAT` (a runtime-populated dict in `runtime.py`) to determine the response format. The spec (§B.3.6) says the extractors should take a `response_format: str` parameter instead — but that's a **signature change** that requires updating every call site, and the provider registry (which provides `.response_format`) doesn't exist yet (Phase B4).

**For Phase B3:** Move the extractors **with their current signature** (`provider: str` parameter). They still look up the format via a reference to the same `_RESPONSE_FORMAT` dict. This keeps all call sites working unchanged. Phase B4 will introduce the registry and the `response_format: str` parameter switch.

**How to handle `_RESPONSE_FORMAT`:** The new `extractors.py` needs access to this dict. **Import it from runtime.py** at call time (not import time — to avoid circular import). Use a function-level import:

```python
def _get_response_format() -> dict:
    """Get the response format mapping (populated by runtime at startup).

    Lazy import to avoid circular dependency: runtime.py imports this module
    for the extractor functions, so we cannot import runtime at module top.
    """
    from agent.runtime import _RESPONSE_FORMAT
    return _RESPONSE_FORMAT
```

Then in each extractor body, replace `_RESPONSE_FORMAT.get(provider, "openai")` with `_get_response_format().get(provider, "openai")`.

## STEP 1: Discovery (mandatory per Steel-Framed Rule 1)

Read these files in full before writing any code:
1. `prompts/steelFramedCodeWriter.md` — your standing orders
2. `docs/specs/SPEC-RUNTIME-MODULAR-EXTRACTION-PHASE-1.md` §B.3.6 (extractors.py spec), §B.5 (re-exports)
3. `agent/runtime.py` — find and read ALL THREE functions completely:
   - `def _extract_tool_calls` (~line 1080) — extracts tool calls, handles OpenAI + Anthropic
   - `def _extract_text_content` (~line 1134) — extracts text content
   - `def _extract_usage` (~line 1187) — extracts token usage
   - Also read `_RESPONSE_FORMAT` (~line 370) and its population loop (~lines 372-375)
   - Also read `_is_empty_content` (~line 1155) — this STAYS in runtime.py (not moved)
4. `agent/llm/cost.py` and `agent/llm/convert.py` — the modules from Phases B1 and B2 (templates)

**IMPORTANT:** `_is_empty_content` (line ~1155) STAYS in `agent/runtime.py`. It is used at non-extractor sites (placeholder logic at lines ~2320 and ~2442). Do NOT move it. Only move the three extractor functions.

## Deliverable — 3 files

### File 1: `agent/llm/extractors.py` (NEW)

Move these three functions VERBATIM from `agent/runtime.py`:

| Old name (runtime.py) | New name (extractors.py) |
|---|---|
| `_extract_tool_calls` | `extract_tool_calls` |
| `_extract_text_content` | `extract_text_content` |
| `_extract_usage` | `extract_usage` |

**Drop the leading underscores.**

**Required imports:**
- `import json` — used by `_extract_tool_calls` for `json.loads(args_raw)`
- `import uuid` — used by `_extract_tool_calls` for `uuid.uuid4().hex` in the synthetic call_id fallback
- `import logging` + `logger = logging.getLogger(__name__)` — if any logging is in the bodies (check)

**Response format lookup:** Add the `_get_response_format()` lazy-import helper (shown above). Replace the three `_RESPONSE_FORMAT.get(provider, "openai")` calls with `_get_response_format().get(provider, "openai")`.

Module structure:
```python
"""Response extractors for LLM API responses.

Extracted from agent/runtime.py (Phase B3). Parses tool calls, text content,
and token usage from both OpenAI and Anthropic response formats. Pure functions
except for the response_format lookup (lazy import from runtime).

Note: _is_empty_content stays in runtime.py — it is used at non-extractor sites.
"""

from __future__ import annotations

import json
import logging
import uuid

logger = logging.getLogger(__name__)


def _get_response_format() -> dict:
    """Get the response format mapping (populated by runtime at startup).

    Lazy import to avoid circular dependency: runtime.py imports this module
    for the extractor functions, so we cannot import runtime at module top.
    """
    from agent.runtime import _RESPONSE_FORMAT
    return _RESPONSE_FORMAT


def extract_tool_calls(response: dict, provider: str) -> list[tuple[str, str, dict]]:
    # Body moved verbatim, replace _RESPONSE_FORMAT.get with _get_response_format().get
    ...


def extract_text_content(response: dict, provider: str) -> str:
    # Body moved verbatim, replace _RESPONSE_FORMAT.get with _get_response_format().get
    ...


def extract_usage(response: dict, provider: str = "openai") -> tuple[int, int]:
    # Body moved verbatim, replace _RESPONSE_FORMAT.get with _get_response_format().get
    ...
```

### File 2: Edit `agent/runtime.py` — replace extractor defs with re-exports

Find the three function definitions. They are adjacent (lines ~1080-1210). `_is_empty_content` sits between `_extract_text_content` and `_extract_usage` — **leave it in place**. Delete ONLY the three extractor function definitions and replace with a re-export block:

```python
# ── Response extractors (extracted to agent/llm/extractors.py, Phase B3) ────
# Re-exported under legacy underscore names for backward compatibility.
# _is_empty_content stays here (used at non-extractor sites).
from agent.llm.extractors import (
    extract_tool_calls as _extract_tool_calls,
    extract_text_content as _extract_text_content,
    extract_usage as _extract_usage,
)
```

**Placement:** The re-export block goes where `_extract_tool_calls` was (before `_is_empty_content`). `_is_empty_content` stays where it is. The order in runtime.py becomes: re-export block → `_is_empty_content` (stays) → `_format_chunks_for_llm` (stays) → conversation persistence (stays).

### File 3: `tests/test_llm_extractors.py` (NEW)

Write tests for the extracted functions. Test the PUBLIC names (no underscore):

**Test cases for `extract_tool_calls` (spec §B.9 cases 23-25):**
1. `test_extract_tool_calls_openai_format` — choices[0].message.tool_calls parsed into (call_id, name, args) tuples
2. `test_extract_tool_calls_anthropic_format` — content blocks with type=tool_use parsed
3. `test_extract_tool_calls_empty_choices` — no choices → empty list

**Test cases for `extract_text_content` (spec §B.9 cases 26-27):**
4. `test_extract_text_content_openai` — choices[0].message.content returned
5. `test_extract_text_content_anthropic` — text blocks joined into a string

**Test cases for `extract_usage` (spec §B.9 cases 28-30):**
6. `test_extract_usage_openai` — prompt_tokens/completion_tokens returned
7. `test_extract_usage_anthropic` — input_tokens/output_tokens returned
8. `test_extract_usage_missing` — no usage key → (0, 0)

**Sad-path tests:**
9. `test_extract_tool_calls_synthetic_id_for_empty_id` — empty/None id → synthetic call_XXXXXX id
10. `test_extract_tool_calls_malformed_json_args` — malformed JSON string → raises or kept as-is per verbatim code
11. `test_extract_text_content_empty_choices` — no choices → empty string

**Backward-compat tests:**
12. `test_runtime_reexport_extract_tool_calls` — `from agent.runtime import _extract_tool_calls` works
13. `test_runtime_reexport_extract_text_content` — `from agent.runtime import _extract_text_content` works
14. `test_runtime_reexport_extract_usage` — `from agent.runtime import _extract_usage` works

**IMPORTANT for tests that exercise format lookup:** The `_get_response_format()` function does a lazy import from runtime. To test OpenAI format, you need `_RESPONSE_FORMAT` to have the provider mapped. Either:
- Mock `_get_response_format` to return `{"openai": "openai", "anthropic": "anthropic"}`, OR
- Populate `agent.runtime._RESPONSE_FORMAT` before the test, OR
- Use `unittest.mock.patch` on `agent.llm.extractors._get_response_format`

The cleanest approach is mocking `_get_response_format`. Document your choice in the test docstrings.

## Verification commands (run yourself, paste output in COMPLETENESS)

```bash
# New module imports
python3 -c "from agent.llm.extractors import extract_tool_calls, extract_text_content, extract_usage; print('import OK')"

# runtime.py re-exports work
python3 -c "from agent.runtime import _extract_tool_calls, _extract_text_content, _extract_usage; print('re-export OK')"

# runtime.py compiles
python3 -c "from agent.runtime import AgentRuntime; print('runtime OK')"

# New extractor tests pass (expect 14)
python3 -m pytest tests/test_llm_extractors.py -v

# _is_empty_content still in runtime.py (NOT moved)
grep -c "^def _is_empty_content" agent/runtime.py  # must be 1

# Old extractor defs gone from runtime.py
grep -c "^def _extract_tool_calls\|^def _extract_text_content\|^def _extract_usage" agent/runtime.py  # must be 0

# Re-export block present
grep -c "from agent.llm.extractors import" agent/runtime.py  # must be 1

# runtime.py line count
wc -l agent/runtime.py

# No collateral damage
git diff --name-only agent/tools.py agent/enforcement.py agent/context_strategy.py tests/test_agent_runtime.py  # must be empty
```

## COMPLETENESS checklist (mandatory in your reply)

```
COMPLETENESS:
- [x/not done] STEP 0: Read prompts/steelFramedCodeWriter.md in full — evidence: cite 3 rules applied
- [x/not done] agent/llm/extractors.py created with 3 public functions — evidence: import output
- [x/not done] _get_response_format lazy-import helper added — evidence: grep
- [x/not done] _is_empty_content NOT moved (stays in runtime.py) — evidence: grep count = 1
- [x/not done] agent/runtime.py extractor defs replaced with re-exports — evidence: grep (0 old defs, 1 import block)
- [x/not done] tests/test_llm_extractors.py created with 14 tests — evidence: pytest count
- [x/not done] All new extractor tests pass — evidence: pytest summary
- [x/not done] runtime.py compiles + imports — evidence: python3 -c output
- [x/not done] runtime.py line count — evidence: wc -l
- [x/not done] No collateral damage — evidence: git diff output
```

## Do NOT

- Do NOT move `_is_empty_content` — it stays in `agent/runtime.py`.
- Do NOT modify `_RESPONSE_FORMAT` or its population loop in runtime.py.
- Do NOT change the extractor function signatures yet (keep `provider: str` parameter; the `response_format: str` switch is Phase B4).
- Do NOT modify any call site in runtime.py (lines 2162-2166, 2304-2310, 3205-3206).
- Do NOT modify `tests/test_agent_runtime.py`.
- Do NOT modify `agent/tools.py`, `agent/enforcement.py`, `agent/context_strategy.py`.
- Do NOT change the extraction logic — move it verbatim (only: rename, replace `_RESPONSE_FORMAT.get` with `_get_response_format().get`).

## When done

Reply with COMPLETENESS checklist + all verification command outputs pasted verbatim.
