# Phase 4 Instructions — Cost Model Cleanup

**Spec:** `docs/specs/SPEC-RUNTIME-EXTRACTION-PHASE-4.md`
**Files:** `agent/runtime.py` + `tests/test_agent_runtime.py` + `tests/test_llm_cost.py`

**FIRST ACTION: Load the steelFramedCodeWriter prompt fresh.** Read `/path/to/projects/crabcakes/prompts/steelFramedCodeWriter.md` IN FULL. Activate it. Begin with Discovery Phase block.

Read all 3 files before editing. This is a SMALL phase (remove re-export aliases, update call sites).

---

## Edit 1 — `agent/runtime.py`: add direct import

At the top of the file, near the other `from agent.llm.` imports, add:
```python
from agent.llm.cost import cost_for_model
```

## Edit 2 — `agent/runtime.py`: update call site 1 (~line 1332)

Find: `cost = _cost_for_model(conv.model, prompt_tok, comp_tok)`
Change to: `cost = cost_for_model(conv.model, prompt_tok, comp_tok)`

## Edit 3 — `agent/runtime.py`: update call site 2 (~line 1477)

Find: `fb_cost = _cost_for_model(fallback_model, fb_prompt, fb_comp)`
Change to: `fb_cost = cost_for_model(fallback_model, fb_prompt, fb_comp)`

## Edit 4 — `agent/runtime.py`: remove re-export block (~lines 166-175)

Delete the entire block:
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

## Edit 5 — `agent/runtime.py`: remove `_cost_for_model` from `__all__`

Find `"_cost_for_model",` in the `__all__` list and delete that line.

## Edit 6 — `tests/test_agent_runtime.py`: update cost references

Line 22 (import): change `_cost_for_model` to import from `agent.llm.cost`:
```python
from agent.llm.cost import cost_for_model
```
Remove `_cost_for_model` from the `from agent.runtime import (...)` block.

Lines 71, 75, 79: change `_cost_for_model(` to `cost_for_model(`.

## Edit 7 — `tests/test_llm_cost.py`: delete TestRuntimeReexport class

Find the `TestRuntimeReexport` class (around line 44). It exists ONLY to verify the re-export. Delete the entire class (approximately lines 44-56). The canonical cost tests are the other classes in the same file that import from `agent.llm.cost` directly.

---

## Verification

1. `grep -c "_cost_for_model\|_OPENAI_COST\|_MINIMAX_COST\|_ANTHROPIC_COST\|_PROVIDER_COSTS\|_model_id" agent/runtime.py` → **0**
2. `grep -c "cost_for_model(" agent/runtime.py` → **2**
3. `grep -c "_cost_for_model" tests/test_agent_runtime.py` → **0**
4. `grep -c "TestRuntimeReexport" tests/test_llm_cost.py` → **0**
5. `python3 -c "from agent.runtime import AgentRuntime; print('OK')"` → OK
6. `python3 -m pytest tests/test_llm_cost.py -q` → all pass
7. `python3 -m pytest tests/test_agent_runtime.py -q` → all pass (FULL file, no -k filter)

## COMPLETENESS checklist (mandatory)
```
COMPLETENESS:
- [x/not done] Edit 1: Added direct import — evidence: <grep>
- [x/not done] Edit 2: Updated call site 1 — evidence: <grep>
- [x/not done] Edit 3: Updated call site 2 — evidence: <grep>
- [x/not done] Edit 4: Removed re-export block — evidence: <grep -c = 0>
- [x/not done] Edit 5: Removed from __all__ — evidence: <grep>
- [x/not done] Edit 6: Updated test_agent_runtime.py — evidence: <grep>
- [x/not done] Edit 7: Deleted TestRuntimeReexport — evidence: <grep -c = 0>
- [x/not done] All tests pass — evidence: <pytest output>
```
