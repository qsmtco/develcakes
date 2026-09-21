# PHASE C — LLM-Summarization Strategy

**Spec:** `docs/specs/SPEC-CONTEXT-UI-COMPACT-LLM-2026-07-10.md` §3.3
**Files to change:** `agent/context_strategy.py`, `agent/runtime.py`, `agent/special_agents.py`, `utils/agent_defs.py`, `ui/views/agent_builder.py`

---

## EDIT 1 — Add LLMSummarizeStrategy to context_strategy.py

**File:** `agent/context_strategy.py`
**Insertion point:** End of file (after line 741).

Add the `LLMSummarizeStrategy(DefaultContextStrategy)` class per spec §3.3.1. Key points:

- Inherits from `DefaultContextStrategy` — gets Layer 1 (prune) and Layer 2 (trim) for free
- Only overrides `_summary()` (Layer 3)
- Takes `llm_provider: Callable[[str, str], str]` in `__init__`
- Builds a transcript from head messages (everything except tail 4)
- Role-aware truncation: tool messages capped at 500 chars, user/assistant at 2000 chars
- Transcript capped at 8000 chars total
- Uses 9-section structured summary prompt template
- Falls back to `super()._summary()` on any LLM failure (empty response, exception, etc.)
- Optional token-budget enforcement via truncation

Read the spec §3.3.1 for the exact class code. The SUMMARY_PROMPT_TEMPLATE constant and the `_summary` method override are the two main pieces.

---

## EDIT 2 — Add force_llm_compact + _call_for_summary to runtime.py

**File:** `agent/runtime.py`
**Insertion point:** After the `force_compact` method (around line 3008).

### Step A: Add `force_llm_compact` method

Per spec §3.3.2 (with FIX-BUG-3). Key design:

1. Save original strategy: `original_strategy = self._context_strategy`
2. Create `LLMSummarizeStrategy(llm_provider=lambda sys_p, user_p, model_id=None: self._call_for_summary(...))`
3. Swap: `self._context_strategy = strat`
4. Optionally prepend focus_text to conv.system_prompt (restore after)
5. Call `strat.compact(conv, token_budget)`
6. Restore: `self._context_strategy = original_strategy`; restore system_prompt
7. Read `strat.last_result`, return dict with messages_removed/tokens_freed/summary_chars/layer

### Step B: Add `_call_for_summary` method

Per spec §3.3.2 (FIX-BUG-2). Reuses existing runtime infrastructure:

1. Resolve model_id (split on `/` to get provider_name + model)
2. Get provider_cfg from `self._config.providers.get(provider_name)`
3. Build messages: `[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}]`
4. Resolve caller_key via `self._resolve_caller_key(provider_cfg, model)` (static method at line 2519)
5. Get caller from `_PROVIDER_CALLERS.get(caller_key)`
6. Call `caller(base_url=..., api_key=..., model=..., messages=..., tools=None, timeout=..., x_title="crabcakes-summary")`
7. Extract text via `_extract_text_content(response_dict, provider_name)` (line 1208)
8. Return the text string

Read the spec §3.3.2 for exact code. Use `getattr(provider_cfg, "api_key", "") or ""` for api_key resolution.

---

## EDIT 3 — Add compaction_strategy field to SpecialAgentDef

**File:** `agent/special_agents.py`

Add one field to the `SpecialAgentDef` dataclass (around line 50, after existing fields):

```python
    compaction_strategy: str = "textual"  # Phase C — "textual" | "llm"
```

---

## EDIT 4 — Add validation to agent_defs.py

**File:** `utils/agent_defs.py`

In `validate_agent_def` (line 377), add validation for the new field. The function returns `list[str]` (errors), does NOT raise:

```python
    cs = agent_def.get("compaction_strategy", "textual")
    if not isinstance(cs, str) or cs not in {"textual", "llm"}:
        errors.append(
            f"Invalid compaction_strategy: {cs!r}. Must be 'textual' or 'llm'."
        )
```

Read the actual function first to find where `errors` list is built.

---

## EDIT 5 — Add dropdown to agent_builder.py

**File:** `ui/views/agent_builder.py`

Add a compaction strategy dropdown after the existing form fields:

```python
        self._compaction_strategy_combo = Gtk.DropDown.new_from_strings(["textual", "llm"])
        self._compaction_strategy_combo.set_selected(0)
        strat_row = self._labeled_box("Compaction strategy", self._compaction_strategy_combo)
        vbox.append(strat_row)
```

And in the save/get_values callback, add:
```python
            "compaction_strategy": ["textual", "llm"][self._compaction_strategy_combo.get_selected()],
```

Read the file to find the correct insertion points and the existing `get_values` method.

---

## Rules

- Use the `steelFramedCodeWriter` prompt at `prompts/steelFramedCodeWriter.md`.
- Read each file before editing.
- Read the spec §3.3 for exact code.
- Do NOT create `agent/llm_completion.py` — the spec's FIX-BUG-2 removed it. Everything goes in `agent/runtime.py`.
- The `force_llm_compact` method must swap `self._context_strategy` and restore it (FIX-BUG-3).

## Verification

```bash
cd /path/to/projects/crabcakes

# 1. Syntax
python3 -c "import ast; [ast.parse(open(f).read()) for f in ['agent/context_strategy.py', 'agent/runtime.py', 'agent/special_agents.py', 'utils/agent_defs.py', 'ui/views/agent_builder.py']]; print('SYNTAX OK')"

# 2. LLMSummarizeStrategy exists
grep -n "class LLMSummarizeStrategy" agent/context_strategy.py

# 3. force_llm_compact exists
grep -n "def force_llm_compact" agent/runtime.py

# 4. _call_for_summary exists
grep -n "def _call_for_summary" agent/runtime.py

# 5. compaction_strategy field
grep -n "compaction_strategy" agent/special_agents.py

# 6. validation
grep -n "compaction_strategy" utils/agent_defs.py

# 7. dropdown
grep -n "compaction_strategy_combo" ui/views/agent_builder.py

# 8. No llm_completion.py created
ls agent/llm_completion.py 2>&1

# 9. Existing tests pass
python3 -m pytest tests/test_context_strategy.py tests/test_runtime_compaction.py -q -x
```

## Deliverables

```
COMPLETENESS:
- [x/not done] Edit 1: LLMSummarizeStrategy class — evidence: (command 2)
- [x/not done] Edit 2: force_llm_compact + _call_for_summary — evidence: (command 3+4)
- [x/not done] Edit 3: compaction_strategy field — evidence: (command 5)
- [x/not done] Edit 4: validation — evidence: (command 6)
- [x/not done] Edit 5: agent_builder dropdown — evidence: (command 7)
- [x/not done] No llm_completion.py — evidence: (command 8)
- [x/not done] Existing tests pass — evidence: (command 9)
```
