# Phase B1 Instructions — Extract Cost Functions to agent/llm/cost.py

**Track:** B Phase 2a (non-streaming extraction)
**Scope:** Create `agent/llm/cost.py` (NEW), create `agent/llm/__init__.py` (NEW — minimal), add re-export to `agent/runtime.py`, create `tests/test_llm_cost.py` (NEW). Small edits to `agent/runtime.py` only for re-exports.
**Spec reference:** `docs/specs/SPEC-RUNTIME-MODULAR-EXTRACTION-PHASE-1.md` §B.3.8, §B.5
**Rule reference:** `prompts/steelFramedCodeWriter.md` — apply every rule.

## Objective

Extract the cost calculation functions and tables from `agent/runtime.py` into a new `agent/llm/cost.py` module. This is the first extraction in Track B — it proves the pattern (move code, re-export from runtime.py, existing tests pass unchanged). Cost functions are the lowest-risk starting point: pure functions, no streaming, no network, no provider call paths.

## CRITICAL: Read these first

1. `prompts/steelFramedCodeWriter.md` (your standing orders)
2. `docs/specs/SPEC-RUNTIME-MODULAR-EXTRACTION-PHASE-1.md` §B.3.8 (cost.py spec), §B.5 (re-exports)
3. `agent/runtime.py` lines 160-195 — the cost tables and functions you are moving
4. `agent/context_strategy.py` — the template for how an `agent/` module is structured (Protocol → impl → no UI imports)

## Deliverable — 4 files

### File 1: `agent/llm/__init__.py` (NEW — minimal stub)

This file will grow in later phases (B4 will add the full public API). For now, it's a minimal package marker:

```python
"""LLM provider abstraction layer.

This package will host the extracted provider adapters, streaming helpers,
extractors, and cost functions from agent/runtime.py. Phase B1 extracts cost
functions only; subsequent phases add the rest.

Public API (incomplete — grows with each phase):
    cost_for_model(model, prompt_tokens, completion_tokens) -> float
    model_id(model) -> str
"""
```

That's it. No imports yet — just the docstring. Later phases add imports.

### File 2: `agent/llm/cost.py` (NEW)

Move these symbols VERBATIM from `agent/runtime.py` (lines 160-195):

| Old name (runtime.py) | New name (cost.py) |
|---|---|
| `_OPENAI_COST` | `OPENAI_COST` |
| `_MINIMAX_COST` | `MINIMAX_COST` |
| `_ANTHROPIC_COST` | `ANTHROPIC_COST` |
| `_PROVIDER_COSTS` | `PROVIDER_COSTS` |
| `_model_id` | `model_id` |
| `_cost_for_model` | `cost_for_model` |

**Drop the leading underscores** — these are now public within the `agent.llm` package (per spec §B.3.5 naming convention). The re-exports in runtime.py keep the old underscore names as aliases.

The module should look like this (move the code verbatim, rename symbols, keep docstrings):

```python
"""Cost calculation for LLM API calls.

Extracted from agent/runtime.py (Phase B1). Pure functions — no network,
no GTK, no state. The cost tables are USD per 1M tokens.
"""

from __future__ import annotations


# ── Cost tables (USD per 1M tokens) ─────────────────────────────────────────

OPENAI_COST = {"prompt": 2.5, "completion": 10.0}    # GPT-4o
MINIMAX_COST = {"prompt": 0.5, "completion": 1.0}    # MiniMax-M2
ANTHROPIC_COST = {"prompt": 3.0, "completion": 15.0}  # Claude 3.5

PROVIDER_COSTS: dict[str, dict[str, float]] = {
    "openai": OPENAI_COST,
    "minimax": MINIMAX_COST,
    "anthropic": ANTHROPIC_COST,
    "openrouter": OPENAI_COST,  # varies by model, using openai as fallback
    "zai": OPENAI_COST,        # free tier, no cost
}


def model_id(model: str) -> str:
    """Strip the provider prefix, returning the model ID sent to the API.

    'minimax/MiniMax-M2.7'       -> 'MiniMax-M2.7'
    'openrouter/deepseek/deepseek-v4-pro' -> 'deepseek/deepseek-v4-pro'
    """
    parts = model.split("/", 1)
    return parts[1] if len(parts) > 1 else parts[0]


def cost_for_model(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Compute cost in USD for a model call."""
    provider = model.split("/")[0] if "/" in model else model
    costs = PROVIDER_COSTS.get(provider, OPENAI_COST)
    return (prompt_tokens / 1_000_000 * costs["prompt"] +
            completion_tokens / 1_000_000 * costs["completion"])
```

**Important:** The `cost_for_model` function references `PROVIDER_COSTS` and `OPENAI_COST` internally. After renaming, make sure the function body uses the NEW names (no leading underscore). The spec's §B.3.8 table shows the old → new name mapping; the function body must be updated to match.

### File 3: Edit `agent/runtime.py` — replace cost block with re-exports

Find this block in `agent/runtime.py` (currently around lines 160-195):

```python
# ── Cost tables (USD per 1M tokens) ─────────────────────────────────────────

_OPENAI_COST = {"prompt": 2.5, "completion": 10.0}    # GPT-4o
_MINIMAX_COST = {"prompt": 0.5, "completion": 1.0}   # MiniMax-M2
_ANTHROPIC_COST = {"prompt": 3.0, "completion": 15.0} # Claude 3.5

_PROVIDER_COSTS: dict[str, dict[str, float]] = {
    "openai": _OPENAI_COST,
    "minimax": _MINIMAX_COST,
    "anthropic": _ANTHROPIC_COST,
    "openrouter": _OPENAI_COST,  # varies by model, using openai as fallback
    "zai": _OPENAI_COST,        # free tier, no cost
}


def _model_id(model: str) -> str:
    """Strip the provider prefix, returning the model ID sent to the API.

    'minimax/MiniMax-M2.7'       -> 'MiniMax-M2.7'
    'openrouter/deepseek/deepseek-v4-pro' -> 'deepseek/deepseek-v4-pro'
    """
    parts = model.split("/", 1)
    return parts[1] if len(parts) > 1 else parts[0]


def _cost_for_model(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Compute cost in USD for a model call."""
    provider = model.split("/")[0] if "/" in model else model
    costs = _PROVIDER_COSTS.get(provider, _OPENAI_COST)
    return (prompt_tokens / 1_000_000 * costs["prompt"] +
            completion_tokens / 1_000_000 * costs["completion"])
```

**Replace the ENTIRE block** with:

```python
# ── Cost tables + functions (extracted to agent/llm/cost.py, Phase B1) ──────
# Re-exported under legacy underscore names for backward compatibility.
from agent.llm.cost import (
    OPENAI_COST as _OPENAI_COST,
    MINIMAX_COST as _MINIMAX_COST,
    ANTHROPIC_COST as _ANTHROPIC_COST,
    PROVIDER_COSTS as _PROVIDER_COSTS,
    model_id as _model_id,
    cost_for_model as _cost_for_model,
)
```

**Key points:**
- The `as _OLD_NAME` aliases preserve backward compatibility. Tests that `from agent.runtime import _cost_for_model` still work.
- The import goes where the old code was — near the top of the file, after the enforcement import and before the provider adapters section.
- Do NOT remove the `# ── Provider adapters ──` comment that follows — it marks the next section.

### File 4: `tests/test_llm_cost.py` (NEW)

Write tests for the extracted cost module. Test the PUBLIC names (no underscore):

**Test cases (spec §B.9 cases 31-35):**
1. `test_model_id_strips_provider_prefix` — `"minimax/MiniMax-M2.7"` → `"MiniMax-M2.7"`
2. `test_model_id_no_prefix_returns_input` — `"gpt-4o"` → `"gpt-4o"`
3. `test_cost_for_model_openai` — known cost calculation for openai provider
4. `test_cost_for_model_unknown_provider_defaults_openai` — unknown provider falls back to OPENAI_COST
5. `test_cost_for_model_zero_tokens` — zero cost for zero tokens

**Sad-path tests:**
6. `test_cost_for_model_empty_model_string` — empty string → defaults to OPENAI_COST (no crash)
7. `test_model_id_empty_string` — empty string → empty string (no crash)

Also add 2 backward-compatibility tests verifying the re-exports work:
8. `test_runtime_reexport_cost_for_model` — `from agent.runtime import _cost_for_model` works and returns same result
9. `test_runtime_reexport_model_id` — `from agent.runtime import _model_id` works and returns same result

**Import the PUBLIC names in the test file:**
```python
from agent.llm.cost import cost_for_model, model_id, OPENAI_COST, MINIMAX_COST, ANTHROPIC_COST, PROVIDER_COSTS
```

## Verification commands (run yourself, paste output in COMPLETENESS)

```bash
# New module imports
python3 -c "from agent.llm.cost import cost_for_model, model_id, PROVIDER_COSTS; print('import OK')"

# runtime.py still imports (re-exports work)
python3 -c "from agent.runtime import _cost_for_model, _model_id, _PROVIDER_COSTS; print('re-export OK')"

# runtime.py compiles
python3 -c "from agent.runtime import AgentRuntime; print('runtime OK')"

# New cost tests pass (expect 9)
python3 -m pytest tests/test_llm_cost.py -v

# Existing cost tests in test_agent_runtime.py STILL PASS (CRITICAL — no regression)
python3 -m pytest tests/test_agent_runtime.py -o addopts="" -q -k "cost_for_model or CostComputation or model_id" 2>&1 | tail -5

# Old cost code is gone from runtime.py (should be 0 matches for the OLD inline definitions)
grep -c "^def _model_id\|^def _cost_for_model" agent/runtime.py  # must be 0

# Re-export block present
grep -c "from agent.llm.cost import" agent/runtime.py  # must be 1

# runtime.py line count dropped
wc -l agent/runtime.py  # should be ~3260 (was 3314, minus ~37 for the extracted block + 8 for re-export = net ~-29)

# No collateral damage
git diff --name-only agent/tools.py agent/enforcement.py agent/context_strategy.py  # must be empty
```

## COMPLETENESS checklist (mandatory in your reply)

```
COMPLETENESS:
- [x/not done] agent/llm/__init__.py created (minimal stub) — evidence: ls + import
- [x/not done] agent/llm/cost.py created with 6 public symbols — evidence: import output
- [x/not done] agent/runtime.py cost block replaced with re-exports — evidence: grep (0 old defs, 1 import block)
- [x/not done] tests/test_llm_cost.py created with 9 tests — evidence: pytest count
- [x/not done] All new cost tests pass — evidence: pytest summary
- [x/not done] Existing cost tests in test_agent_runtime.py pass (NO regression) — evidence: pytest summary
- [x/not done] runtime.py compiles + imports — evidence: python3 -c output
- [x/not done] runtime.py line count dropped — evidence: wc -l
- [x/not done] No collateral damage — evidence: git diff output
```

## Do NOT

- Do NOT modify any existing test in `tests/test_agent_runtime.py`.
- Do NOT modify `agent/tools.py`, `agent/enforcement.py`, `agent/context_strategy.py`.
- Do NOT remove the `_PROVIDER_COSTS` / `_OPENAI_COST` etc. names from `agent/runtime.py` — they must be re-exported (tests import them).
- Do NOT change the cost calculation logic — move it verbatim (only rename symbols: drop underscores).
- Do NOT touch the tool middleware code (Phase A is done).

## When done

Reply with COMPLETENESS checklist + all verification command outputs pasted verbatim.
