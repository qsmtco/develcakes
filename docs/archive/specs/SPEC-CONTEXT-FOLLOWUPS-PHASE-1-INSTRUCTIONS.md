# PHASE 1 — Context Follow-Ups

**Spec:** `docs/specs/spec-context-followups.md`
**Files to change:** `ui/handlers/project_handler.py`, `utils/agent_defs.py`, `ui/views/agent_builder.py`, `agent/runtime.py`, `ui/handlers/agent_runtime_handler.py`

---

## EDIT 1 — Use target_session_key in cmd_clear and cmd_compact

**File:** `ui/handlers/project_handler.py`

In `cmd_clear` (line 684) AND `cmd_compact` (line 773), replace:
```python
        sk = cmd.source_session_key or session_key
```
with:
```python
        sk = cmd.target_session_key or cmd.source_session_key or session_key
```

Two locations. No other changes to these methods.

---

## EDIT 2 — Add VALID_COMPACTION_STRATEGIES constant

**File:** `utils/agent_defs.py`

Add before `validate_agent_def` (line 377):
```python
VALID_COMPACTION_STRATEGIES: tuple[str, ...] = ("textual", "llm")
```

Update the validation at line 482 to use the constant:
```python
    cs = agent_def.get("compaction_strategy", "textual")
    if not isinstance(cs, str) or cs not in VALID_COMPACTION_STRATEGIES:
        errors.append(
            f"Invalid compaction_strategy: {cs!r}. "
            f"Must be one of: {', '.join(VALID_COMPACTION_STRATEGIES)}."
        )
```

---

## EDIT 3 — Use constant in agent_builder.py

**File:** `ui/views/agent_builder.py`

Add import at top:
```python
from utils.agent_defs import VALID_COMPACTION_STRATEGIES
```

Replace line 136:
```python
        self._compaction_strategy_combo = Gtk.DropDown.new_from_strings(list(VALID_COMPACTION_STRATEGIES))
```

Replace line 192:
```python
            "compaction_strategy":
                list(VALID_COMPACTION_STRATEGIES)[self._compaction_strategy_combo.get_selected()],
```

Line 740 stays as-is (conditional, not list literal).

---

## EDIT 4 — Concurrency guard for force_compact AND force_llm_compact

**File:** `agent/runtime.py`

### Fix A: Wrap force_compact (line 3008) in lock:
```python
    def force_compact(self, conv: "Conversation", token_budget: int) -> None:
        with self._compaction_lock:
            self._context_strategy.compact(conv, token_budget)
```

### Fix B: Wrap force_llm_compact strategy swap in lock:

In `force_llm_compact` (line 3021), wrap the entire swap/compact/restore block in `with self._compaction_lock:`. The `with` goes around the `original_strategy = self._context_strategy` line through the `finally:` block.

---

## EDIT 5 — Per-agent llm_name resolution in force_llm_compact

**File:** `agent/runtime.py`

### Step A: Add agent_def parameter to force_llm_compact:

```python
    def force_llm_compact(
        self,
        conv: "Conversation",
        token_budget: int,
        focus_text: str = "",
        agent_def: Any = None,
    ) -> dict:
```

### Step B: Resolve model_id with correct precedence (llm_name OVERRIDES conv.model):

After the `focus_text` system_prompt modification, before the strategy swap, add:

```python
        # Resolve model_id: agent_def.llm_name takes PRECEDENCE over conv.model.
        resolved_model = None
        if agent_def is not None:
            llm_name = getattr(agent_def, "llm_name", None)
            if llm_name:
                prov_cfg = self._config.providers.get(llm_name)
                if prov_cfg and prov_cfg.default_model:
                    if "/" in prov_cfg.default_model:
                        resolved_model = prov_cfg.default_model
                    else:
                        resolved_model = f"{llm_name}/{prov_cfg.default_model}"
        if not resolved_model:
            resolved_model = conv.model
```

### Step C: Use resolved_model in the lambda:

Change the lambda's `model_id=model_id or conv.model` to `model_id=model_id or resolved_model`.

---

## EDIT 6 — Pass agent_def in compact_conversation

**File:** `ui/handlers/agent_runtime_handler.py`

In `compact_conversation` (around line 506), change:
```python
                return rt.force_llm_compact(conv, target_budget, focus_text)
```
to:
```python
                return rt.force_llm_compact(conv, target_budget, focus_text, agent_def=agent_def)
```

---

## Rules

- Use the `steelFramedCodeWriter` prompt at `prompts/steelFramedCodeWriter.md`.
- Read each file before editing.
- Do NOT touch `ui/handlers/command_handler.py` — @mention parsing already works.
- Do NOT touch line 740 in agent_builder.py — it uses a conditional, not a list literal.

## Verification

```bash
cd /path/to/projects/crabcakes

# 1. Syntax
python3 -c "import ast; [ast.parse(open(f).read()) for f in ['ui/handlers/project_handler.py', 'utils/agent_defs.py', 'ui/views/agent_builder.py', 'agent/runtime.py', 'ui/handlers/agent_runtime_handler.py']]; print('SYNTAX OK')"

# 2. target_session_key used
grep -n "target_session_key" ui/handlers/project_handler.py

# 3. VALID_COMPACTION_STRATEGIES exists
grep -n "VALID_COMPACTION_STRATEGIES" utils/agent_defs.py

# 4. No hardcoded list in agent_builder
grep -c '"textual", "llm"' ui/views/agent_builder.py

# 5. force_compact has lock
grep -A2 "def force_compact" agent/runtime.py | grep "_compaction_lock"

# 6. force_llm_compact has lock
grep -A5 "def force_llm_compact" agent/runtime.py | grep "_compaction_lock"

# 7. agent_def parameter
grep -n "agent_def" agent/runtime.py | grep force_llm_compact

# 8. Existing tests
python3 -m pytest tests/test_project_handler.py tests/test_command_handler.py tests/test_compact_command.py tests/test_llm_summarize_strategy.py -q
```
