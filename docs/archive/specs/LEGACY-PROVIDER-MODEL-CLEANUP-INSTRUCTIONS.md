# PHASE INSTRUCTIONS: Remove Legacy provider/model Dead Code

**Date:** 2026-06-14
**Supervisor:** Qaster
**Builder:** QTR

## Context

The `llm_name` field replaced `provider` in agent YAMLs. Existing code has backward-compat fallbacks (`or agent_def.get("provider")`) that are no longer needed. This task removes ALL legacy `provider`/`model` dead code across 12 files in 15 items.

Each item is a separate phase. Each phase must be independently verified before moving to the next.

## Rules

- Use the `steelFramedCodeWriter` prompt at `prompts/steelFramedCodeWriter.md`
- Run `pytest tests/ -x -q --tb=short` after each phase and paste the output
- For any removal: run `grep -n` to prove the pattern is gone, paste the output
- Report: files changed with line numbers, test results, grep proof, any issues
- Do NOT fix unrelated issues — flag them only
- At the end, include a COMPLETENESS checklist for each item in this phase

---

## PHASE 1 of 15 — `agent/special_agents.py`: Remove `or agent_def.get("provider")`

**File:** `agent/special_agents.py`

In `_load_registry()`, change:
```python
llm_name=agent_def.get("llm_name") or agent_def.get("provider"),
model=agent_def.get("model"),
```
To:
```python
llm_name=agent_def.get("llm_name"),
# model removed — runtime resolves from providers.yaml via llm_name
```

Also remove `model: str | None = None` from the `SpecialAgentDef` dataclass (it's a field, not just a default — remove the field entirely).

**Verification grep:**
```bash
grep -n 'agent_def.get("provider")' agent/special_agents.py
# Expected: 0 matches
grep -n 'model=' agent/special_agents.py
# Expected: 0 matches
grep -n 'model:' agent/special_agents.py
# Expected: 0 matches
```

**Then run:**
```bash
pytest tests/test_special_agents.py -x -q --tb=short
```

---

## PHASE 2 of 15 — `agent/special_agents.py`: Remove `api_key_built_in` dead code

This is part of the same file, done after Phase 1.

In `_load_registry()`, the line:
```python
api_key=agent_def.get("api_key_built_in") and agent_def.get("api_key", "") or "",
```
This is dead code — `api_key_built_in` is always False and `api_key` is never set on agent defs. Replace with:
```python
api_key=None,
```

Also remove `api_key_built_in: bool = False` from the `SpecialAgentDef` dataclass.

**Verification grep:**
```bash
grep -n 'api_key_built_in' agent/special_agents.py
# Expected: 0 matches
grep -n 'agent_def.get("api_key"' agent/special_agents.py
# Expected: 0 matches
```

---

## PHASE 3 of 15 — `utils/agent_defs.py`: Remove `or agent_def.get("provider")` from validation

**File:** `utils/agent_defs.py`

In `validate_agent_def()`, change:
```python
if not (agent_def.get("llm_name") or agent_def.get("provider")):
    errors.append("Missing required field: llm_name")
```
To:
```python
if not agent_def.get("llm_name"):
    errors.append("Missing required field: llm_name")
```

**Verification grep:**
```bash
grep -n 'agent_def.get("provider")' utils/agent_defs.py
# Expected: 0 matches
```

**Then run:**
```bash
pytest tests/test_agent_defs.py -x -q --tb=short
```

---

## PHASE 4 of 15 — `utils/agent_defs.py`: Remove `model` validation check

In `validate_agent_def()`, find and remove the block that checks for model presence:
```python
# Validate model is present (could come from provider default)
if not agent_def.get("model") and provider:
    providers = get_available_providers()
    for p in providers:
        p_id = p["default_model"].split("/")[0] if p.get("default_model") and "/" in p.get("default_model", "") else p["name"]
        if p["name"] == provider or p_id == provider:
            if not p.get("default_model"):
                errors.append(f"No model specified and provider '{provider}' has no default")
            break
```

Remove the entire block. Also remove the `provider` variable assignment above it (the line `provider = agent_def.get("llm_name") or agent_def.get("provider")` — replace with just `provider = agent_def.get("llm_name")` — wait, actually the `provider` variable is only used in the model check. If the model check is removed, the `provider` variable is unused. Remove it entirely.)

Actually: check what the `provider` variable is used for after this block. It also appears in the provider validation block above:
```python
provider = agent_def.get("llm_name") or agent_def.get("provider")
if provider:
    providers = get_available_providers()
    ...
```

Change this to:
```python
llm_name = agent_def.get("llm_name")
if llm_name:
    providers = get_available_providers()
    valid_ids = set()
    display_names = set()
    for p in providers:
        display_names.add(p["name"])
        valid_ids.add(p["name"])
        if p.get("default_model") and "/" in p.get("default_model"):
            valid_ids.add(p["default_model"].split("/")[0])
    if display_names and llm_name not in valid_ids:
        errors.append(f"Unknown provider: {llm_name}. Available: {', '.join(sorted(display_names))}")
```

**Verification grep:**
```bash
grep -n 'agent_def.get("model")' utils/agent_defs.py
# Expected: 0 matches
grep -n 'agent_def.get("provider")' utils/agent_defs.py
# Expected: 0 matches
```

---

## PHASE 5 of 15 — `utils/agent_defs.py`: Remove `load_agent_def_by_role` compat

In `load_agent_def_by_role()`, currently does NOT reference `provider` — verify this is already clean.

**Verification grep:**
```bash
grep -n 'load_agent_def_by_role' utils/agent_defs.py
# Check the function body — if it doesn't reference 'provider', it's fine
```

Also remove the `_migrate_legacy_agent_names()` function entirely (PHASE 11 — do it here since we're in the same file). Remove both the function definition AND its call site in `load_agent_defs()`.

```bash
grep -n '_migrate_legacy_agent_names' utils/agent_defs.py
# Expected: 0 matches after removal
```

---

## PHASE 6 of 15 — `ui/handlers/agent_runtime_handler.py`: Remove provider/model getattr

**File:** `ui/handlers/agent_runtime_handler.py`

In `_resolve_agent_model()`, change:
```python
provider = getattr(agent_def, "llm_name", None) or getattr(agent_def, "provider", None)
model = getattr(agent_def, "model", None)
```
To:
```python
llm_name = getattr(agent_def, "llm_name", None)
```

Then update the rest of the method to use `llm_name` instead of `provider`, and remove all branches referencing `model:
```python
# No overrides → use global default
if not provider and not model:
    return None
```
→
```python
if not llm_name:
    return None
```

Remove the "Model already has provider prefix" branch:
```python
if model and "/" in model:
    return model
```

Remove the "Both set → combine" branch:
```python
if provider and model:
    return f"{provider}/{model}"
```

Rename the "Only provider set" branch to "Only llm_name set":
```python
if llm_name:
    try:
        from agent.config import load_agent_config
        config = load_agent_config()
        prov_cfg = config.providers.get(llm_name)
        ...
```

Remove the "Only model set" branch entirely.

**Verification grep:**
```bash
grep -n 'getattr(agent_def, "provider"' ui/handlers/agent_runtime_handler.py
# Expected: 0 matches
grep -n 'getattr(agent_def, "model"' ui/handlers/agent_runtime_handler.py
# Expected: 0 matches
grep -n 'agent_def.provider' ui/handlers/agent_runtime_handler.py
# Expected: 0 matches
grep -n 'agent_def.model' ui/handlers/agent_runtime_handler.py
# Expected: 0 matches
```

**Then run:**
```bash
pytest tests/test_agent_runtime.py -x -q --tb=short
```

---

## PHASE 7 of 15 — `ui/handlers/agent_runtime_handler.py`: Remove `model:` from SpecialAgentDef usage (verify)

Check `_resolve_agent_model()` for any remaining references to `agent_def.model` or the `model` field of `SpecialAgentDef`. Remove if found.

Also verify: `send_to_special_agent()` uses `agent_model = self._resolve_agent_model(agent_def)` — this is fine, keep it.

**Verification grep:**
```bash
grep -n '\.model' ui/handlers/agent_runtime_handler.py
# Expected: only in agent_model = self._resolve_agent_model(agent_def) line
```

---

## PHASE 8 of 15 — `ui/views/agent_builder.py`: Remove legacy provider field

**File:** `ui/views/agent_builder.py`

In `get_values()`, verify that `provider` key is NOT emitted and `model` key is NOT emitted. The current code should already use `llm_name`.

**Verification grep:**
```bash
grep -n '"provider"' ui/views/agent_builder.py
# Expected: 0 matches
grep -n '"model"' ui/views/agent_builder.py
# Expected: 0 matches
grep -n '_get_selected_provider_id\|_get_selected_model' ui/views/agent_builder.py
# If _get_selected_provider_id exists, rename to _get_selected_llm_name
# If _get_selected_model exists, remove it
```

**Then run:**
```bash
pytest tests/test_agent_builder_dialog.py -x -q --tb=short
```

---

## PHASE 9 of 15 — `agent/config.py`: Delete agent.json providers fallback

**File:** `agent/config.py`

In `_load_providers_from_yaml_or_fallback()`, delete the entire "Attempt 2: agent.json providers section (fallback)" block (currently lines ~176-196):
```python
    # Attempt 2: agent.json providers section (fallback)
    providers: dict[str, LLMProviderConfig] = {}
    raw_providers = raw.get("providers", {})
    if raw_providers:
        logger.warning(...)
        for name, prov in raw_providers.items():
            ...
    return providers
```

Replace the entire fallback block's return with just:
```python
    return {}
```

Also update the docstring to remove mention of the agent.json fallback.

**Verification grep:**
```bash
grep -n 'agent.json.*providers\|Attempt 2\|fallback' agent/config.py
# Expected: 0 matches
grep -n 'raw_providers' agent/config.py
# Expected: 0 matches
```

**Then run:**
```bash
pytest tests/test_config.py -x -q --tb=short
```

---

## PHASE 10 of 15 — `agent/runtime.py`: Remove steps 2 and 3 from `_resolve_caller_key`

**File:** `agent/runtime.py`

In `_resolve_caller_key()`, currently has 3 resolution steps. Remove steps 2 and 3. After change, the method should be:

```python
@staticmethod
def _resolve_caller_key(provider_cfg: "LLMProviderConfig | None", model: str) -> str:
    """Return the API caller key for a provider.

    Resolution order:
    1. provider_cfg.caller (explicit, persisted in providers.yaml)

    Returns the empty string if caller is empty — the caller will then fail
    with a clear "no caller" error.
    """
    if provider_cfg is not None and provider_cfg.caller:
        return provider_cfg.caller.lower()
    # No caller configured — return empty string for clear error upstream
    return ""
```

**Verification grep:**
```bash
grep -n 'default_model.*split\|model.*split\|_resolve_caller_key' agent/runtime.py
# Expected: _resolve_caller_key definition only, no split logic
```

**Then run:**
```bash
pytest tests/test_runtime_caller_resolution.py tests/test_runtime_fallback.py -x -q --tb=short
```

---

## PHASE 11 of 15 — `utils/agent_defs.py`: Delete `_migrate_legacy_agent_names()`

**File:** `utils/agent_defs.py`

Remove `_migrate_legacy_agent_names()` function entirely AND its call in `load_agent_defs()`:
```python
_migrate_legacy_agent_names()
```

**Verification grep:**
```bash
grep -n '_migrate_legacy_agent_names' utils/agent_defs.py
# Expected: 0 matches
```

**Then run:**
```bash
pytest tests/test_agent_defs.py -x -q --tb=short
```

---

## PHASE 12 of 15 — `scripts/migrate_provider_caller.py`: Delete file

**File:** `scripts/migrate_provider_caller.py`

Delete the entire file. It was a one-shot migration that already ran.

```bash
ls scripts/migrate_provider_caller.py
# Expected: FileNotFoundError or "No such file"
```

---

## PHASE 13 of 15 — `utils/project_awareness.py`: Remove `_migrate_or_empty_team`

**File:** `utils/project_awareness.py`

Replace `_migrate_or_empty_team()` with a direct `ProjectTeam()` creation. Remove the function definition and update call sites:

In `_setup_crabcakes_dir()` and any other call site, replace:
```python
team = _migrate_or_empty_team(project_path, project_name, pm_name, pm_id)
```
with:
```python
team = ProjectTeam(pm_name=pm_name, pm_id=pm_id)
```

Ensure `ProjectTeam` import is present (it already is).

**Verification grep:**
```bash
grep -n '_migrate_or_empty_team' utils/project_awareness.py
# Expected: 0 matches
```

**Then run:**
```bash
pytest tests/test_project_awareness.py -x -q --tb=short
```

---

## PHASE 14 of 15 — `utils/project_awareness.py`: Remove `_migrate_or_create_manifest`

**File:** `utils/project_awareness.py`

Replace `_migrate_or_create_manifest(project_path, project_name)` with direct manifest creation. The function creates `project.md` from a legacy `crabcakes.md` or generates a skeleton. Replace with:

```python
_generate_project_manifest(project_path, project_name)
```

Wait — rename it to `_create_project_manifest()` (just a rename, same function body minus the migration check):

Actually: the function body IS the migration. The function checks for old `crabcakes.md`, if found migrates it, otherwise generates a skeleton. Remove the migration part and keep only the skeleton generation:

Replace the function with:
```python
def _create_project_manifest(project_path: str, project_name: str) -> None:
    """Create .crabcakes/project.md with a basic structure."""
    manifest_path = os.path.join(get_crabcakes_dir(project_path), MANIFEST_FILENAME)
    if os.path.isfile(manifest_path):
        return  # Already exists, don't overwrite
    generate_project_skeleton(project_path, project_name)
```

Update all call sites consistently.

**Verification grep:**
```bash
grep -n '_migrate_or_create_manifest' utils/project_awareness.py
# Expected: 0 matches
```

**Then run:**
```bash
pytest tests/test_project_awareness.py tests/test_create_project.py -x -q --tb=short
```

---

## PHASE 15 of 15 — On-disk `debugger.yaml`: Remove provider/model keys

**File:** `~/.config/crabcakes/agents/debugger.yaml`

Manually edit to remove `provider: minimax` and `model: MiniMax-M2.7` and add `llm_name: MiniMax M2.7` (use whatever display name the user's providers.yaml has for this provider — check first with `python3 -c "from utils.providers_store import load_providers; [print(p.name) for p in load_providers()]"`).

If `llm_name` is already present, just remove `provider:` and `model:`.

Also check and update `~/.config/crabcakes/agents/coder.yaml` if it has `provider:` or `model:` keys.

**Verification:**
```bash
grep -n 'provider:' ~/.config/crabcakes/agents/debugger.yaml
# Expected: 0 matches
grep -n 'model:' ~/.config/crabcakes/agents/debugger.yaml
# Expected: 0 matches
grep -n 'llm_name:' ~/.config/crabcakes/agents/debugger.yaml
# Expected: 1 match
```

---

## FINAL VERIFICATION (after all 15 phases)

```bash
# 1. No legacy provider references in source (excluding comments/docs)
grep -rn 'agent_def.get("provider")\|\.provider\b\|getattr.*"provider"' \
  agent/special_agents.py utils/agent_defs.py ui/handlers/agent_runtime_handler.py \
  ui/views/agent_builder.py agent/config.py agent/runtime.py utils/project_awareness.py
# Expected: 0 matches

# 2. No model= in special_agents.py
grep -n 'model=' agent/special_agents.py
# Expected: 0 matches

# 3. migrate_provider_caller.py deleted
ls scripts/migrate_provider_caller.py 2>&1
# Expected: No such file

# 4. _migrate_legacy_agent_names gone
grep -rn '_migrate_legacy_agent_names' utils/
# Expected: 0 matches

# 5. _migrate_or_empty_team and _migrate_or_create_manifest gone
grep -rn '_migrate_or_empty_team\|_migrate_or_create_manifest' utils/
# Expected: 0 matches

# 6. Full test suite
pytest tests/ -x -q --tb=short
# Expected: all pass (or same pre-existing failures as before)
```

---

## COMPLETENESS CHECKLIST (include at end of each phase report)

- [ ] Phase 1: `agent/special_agents.py` — `or agent_def.get("provider")` removed, `model` field removed from SpecialAgentDef
- [ ] Phase 2: `agent/special_agents.py` — `api_key_built_in` dead code removed
- [ ] Phase 3: `utils/agent_defs.py` — `or agent_def.get("provider")` removed from validation
- [ ] Phase 4: `utils/agent_defs.py` — `model` validation block removed
- [ ] Phase 5: `utils/agent_defs.py` — `_migrate_legacy_agent_names()` deleted + call site removed
- [ ] Phase 6: `ui/handlers/agent_runtime_handler.py` — `provider`/`model` getattr removed from `_resolve_agent_model()`
- [ ] Phase 7: `ui/handlers/agent_runtime_handler.py` — verify no remaining `.model` references
- [ ] Phase 8: `ui/views/agent_builder.py` — verify no `provider`/`model` key emission
- [ ] Phase 9: `agent/config.py` — agent.json providers fallback block deleted
- [ ] Phase 10: `agent/runtime.py` — steps 2+3 removed from `_resolve_caller_key()`
- [ ] Phase 11: `utils/agent_defs.py` — `_migrate_legacy_agent_names()` deleted (if not done in Phase 5)
- [ ] Phase 12: `scripts/migrate_provider_caller.py` — file deleted
- [ ] Phase 13: `utils/project_awareness.py` — `_migrate_or_empty_team` replaced with direct `ProjectTeam()`
- [ ] Phase 14: `utils/project_awareness.py` — `_migrate_or_create_manifest` replaced with `_create_project_manifest`
- [ ] Phase 15: `~/.config/crabcakes/agents/debugger.yaml` — `provider:`/`model:` removed, `llm_name:` present
