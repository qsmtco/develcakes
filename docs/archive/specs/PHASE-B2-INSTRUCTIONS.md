# Phase B2 Instructions — Extract Anthropic Converters to agent/llm/convert.py

**Track:** B Phase 2a (non-streaming extraction)
**Scope:** Create `agent/llm/convert.py` (NEW), edit `agent/runtime.py` (re-export block), create `tests/test_llm_convert.py` (NEW).
**Spec reference:** `docs/specs/SPEC-RUNTIME-MODULAR-EXTRACTION-PHASE-1.md` §B.3.7, §B.5
**Rule reference:** `prompts/steelFramedCodeWriter.md` — **READ THIS FILE IN FULL FIRST. It is mandatory.**

## STEP 0 (MANDATORY — do this before writing any code)

Read `prompts/steelFramedCodeWriter.md` in full. It defines the discovery block you must produce before writing code (Rule 1: Read Before You Write), the hard-part-first principle, and the verify-every-claim rule. Your COMPLETENESS checklist must cite which Steel-Framed rules you applied.

## Objective

Extract the two Anthropic-format converter functions from `agent/runtime.py` into a new `agent/llm/convert.py` module. These are called only by `_call_anthropic` (and will later be called by `AnthropicProvider`). Keeping them in a separate module makes them testable in isolation and available to future Anthropic-compatible providers.

## STEP 1: Discovery (mandatory per Steel-Framed Rule 1)

Read these files in full before writing any code:
1. `prompts/steelFramedCodeWriter.md` — your standing orders
2. `docs/specs/SPEC-RUNTIME-MODULAR-EXTRACTION-PHASE-1.md` §B.3.7 (convert.py spec), §B.5 (re-exports)
3. `agent/runtime.py` — find `def _convert_messages_for_anthropic` (currently ~line 265) and `def _convert_tools_for_anthropic` (currently ~line 317). Read both functions completely.
4. `agent/llm/cost.py` — the module from Phase B1. This is the template for how an extracted module looks.

## Deliverable — 3 files

### File 1: `agent/llm/convert.py` (NEW)

Move these two functions VERBATIM from `agent/runtime.py`:

| Old name (runtime.py) | New name (convert.py) |
|---|---|
| `_convert_messages_for_anthropic` | `convert_messages_for_anthropic` |
| `_convert_tools_for_anthropic` | `convert_tools_for_anthropic` |

**Drop the leading underscores** — these are now public within the `agent.llm` package.

**IMPORTANT — the `json` import:** `_convert_messages_for_anthropic` uses `json.loads` (inside the tool-call args parsing block). The new module must import `json` at the top. The `logger.debug` call also requires a logger — add `import logging` and `logger = logging.getLogger(__name__)`.

**IMPORTANT — the `logger` reference:** The function body has a `logger.debug("Failed to parse tool-call args JSON: %s", e)` call. You need a module-level logger. Add:
```python
import logging
logger = logging.getLogger(__name__)
```

Module structure:
```python
"""Anthropic message and tool format converters.

Extracted from agent/runtime.py (Phase B2). Pure functions — convert
OpenAI-format message/tool dicts to Anthropic's content-block format.
No network, no GTK, no state.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)


def convert_messages_for_anthropic(messages: list[dict]) -> list[dict]:
    # Body moved verbatim from runtime.py _convert_messages_for_anthropic
    ...


def convert_tools_for_anthropic(tools: list[dict]) -> list[dict]:
    # Body moved verbatim from runtime.py _convert_tools_for_anthropic
    ...
```

### File 2: Edit `agent/runtime.py` — replace converter defs with re-exports

Find the two function definitions (`def _convert_messages_for_anthropic` and `def _convert_tools_for_anthropic`). They are adjacent (lines ~265-338 currently). **Delete both function definitions** and replace with a re-export block:

```python
# ── Anthropic converters (extracted to agent/llm/convert.py, Phase B2) ──────
# Re-exported under legacy underscore names for backward compatibility.
from agent.llm.convert import (
    convert_messages_for_anthropic as _convert_messages_for_anthropic,
    convert_tools_for_anthropic as _convert_tools_for_anthropic,
)
```

Place the re-export block where the old functions were (between the MiniMax caller and the Anthropic caller). The surrounding functions (`_call_minimax` above, `_call_anthropic` below) must remain untouched.

**Key points:**
- The `as _OLD_NAME` aliases preserve backward compatibility.
- `_call_anthropic` (the function below) calls `_convert_messages_for_anthropic` and `_convert_tools_for_anthropic` — these still resolve correctly via the re-exports.
- Do NOT modify `_call_anthropic` itself.

### File 3: `tests/test_llm_convert.py` (NEW)

Write tests for the extracted converters. Test the PUBLIC names (no underscore):

**Test cases for `convert_messages_for_anthropic`:**
1. `test_convert_system_message_to_user` — system role → user role with same content
2. `test_convert_system_message_empty_content_skipped` — system with empty content → not included in output
3. `test_convert_user_message_passthrough` — user role without tool_calls → passed through as-is
4. `test_convert_assistant_with_tool_calls` — assistant with tool_calls → content blocks with tool_use type
5. `test_convert_assistant_with_text_and_tool_calls` — assistant with content + tool_calls → text block + tool_use blocks
6. `test_convert_tool_message_to_tool_result` — tool role → user role with tool_result content block
7. `test_convert_tool_call_args_json_parsing` — string args → parsed to dict in input field
8. `test_convert_tool_call_args_already_dict` — dict args → passed through as-is
9. `test_convert_malformed_tool_call_args_json` — malformed JSON string → kept as string (no crash, logged)

**Test cases for `convert_tools_for_anthropic`:**
10. `test_convert_tools_basic` — function dict with name/description/parameters → name/description/input_schema
11. `test_convert_tools_missing_description_defaults_empty` — missing description → ""
12. `test_convert_tools_none_parameters_defaults_empty_dict` — None parameters → {}
13. `test_convert_tools_non_dict_parameters_defaults_empty_dict` — non-dict parameters → {}

**Backward-compat tests:**
14. `test_runtime_reexport_convert_messages` — `from agent.runtime import _convert_messages_for_anthropic` works
15. `test_runtime_reexport_convert_tools` — `from agent.runtime import _convert_tools_for_anthropic` works

## Verification commands (run yourself, paste output in COMPLETENESS)

```bash
# New module imports
python3 -c "from agent.llm.convert import convert_messages_for_anthropic, convert_tools_for_anthropic; print('import OK')"

# runtime.py re-exports work
python3 -c "from agent.runtime import _convert_messages_for_anthropic, _convert_tools_for_anthropic; print('re-export OK')"

# runtime.py compiles
python3 -c "from agent.runtime import AgentRuntime; print('runtime OK')"

# New convert tests pass (expect 15)
python3 -m pytest tests/test_llm_convert.py -v

# Existing anthropic tests in test_agent_runtime.py STILL PASS (regression check)
python3 -m pytest tests/test_agent_runtime.py -o addopts="" -q -k "anthropic or Anthropic" 2>&1 | tail -5

# Old defs gone from runtime.py
grep -c "^def _convert_messages_for_anthropic\|^def _convert_tools_for_anthropic" agent/runtime.py  # must be 0

# Re-export block present
grep -c "from agent.llm.convert import" agent/runtime.py  # must be 1

# runtime.py line count
wc -l agent/runtime.py

# No collateral damage
git diff --name-only agent/tools.py agent/enforcement.py agent/context_strategy.py tests/test_agent_runtime.py  # must be empty
```

## COMPLETENESS checklist (mandatory in your reply)

```
COMPLETENESS:
- [x/not done] STEP 0: Read prompts/steelFramedCodeWriter.md in full — evidence: cite 3 rules you applied
- [x/not done] agent/llm/convert.py created with 2 public functions — evidence: import output
- [x/not done] agent/runtime.py converter defs replaced with re-exports — evidence: grep (0 old defs, 1 import block)
- [x/not done] tests/test_llm_convert.py created with 15 tests — evidence: pytest count
- [x/not done] All new convert tests pass — evidence: pytest summary
- [x/not done] Existing anthropic tests pass (NO regression) — evidence: pytest summary
- [x/not done] runtime.py compiles + imports — evidence: python3 -c output
- [x/not done] runtime.py line count — evidence: wc -l
- [x/not done] No collateral damage — evidence: git diff output
```

## Do NOT

- Do NOT modify `_call_anthropic`, `_call_openai`, `_call_minimax`, or any other existing function in runtime.py.
- Do NOT modify `tests/test_agent_runtime.py`.
- Do NOT modify `agent/tools.py`, `agent/enforcement.py`, `agent/context_strategy.py`.
- Do NOT change the converter logic — move it verbatim (only rename: drop underscores).
- Do NOT touch the cost module (Phase B1 is done) or the tool middleware (Phase A is done).

## When done

Reply with COMPLETENESS checklist + all verification command outputs pasted verbatim.
