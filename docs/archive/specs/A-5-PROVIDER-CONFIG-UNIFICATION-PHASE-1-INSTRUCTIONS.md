# A-5: Provider Config Unification — Phase 1 of 1

**Supervisor:** Qaster
**Date:** 2026-06-19
**Scope:** Consolidate provider config to one source of truth (`providers.yaml`). Remove the `agent.json` `providers` field entirely.
**Builder:** QTR — use `prompts/steelFramedCodeWriter.md` for all code changes
**Required word marker:** "please write"

---

## Background

Provider config currently lives in two places:
- **`providers.yaml`** (current, in `utils/providers_store.py`) — read by `agent/config.py:_load_providers_from_yaml` and `utils/agent_defs.py:get_available_providers`. Written by `ui/handlers/settings_handler.py` and `ui/handlers/auxilium_wizard_handler.py`.
- **`agent.json` `providers` field** (legacy) — read by `utils/agent_defs.py:save_provider` and `delete_provider` (they write back to the same `agent.json` `providers` key). Written by `ui/handlers/agent_builder_handler.py`.

**User direction:** Consolidate to one source of truth. Do NOT preserve backwards compatibility. The `agent.json` `providers` field is removed entirely. Any providers currently in `agent.json` must be migrated to `providers.yaml` on startup, then the `providers` key is deleted from `agent.json` (the rest of `agent.json` stays).

**Net result after this phase:**
- `providers.yaml` is the only store.
- `agent.json` no longer has a `providers` key.
- `save_provider` and `delete_provider` in `utils/agent_defs.py` are gone.
- `agent_builder_handler.py` calls `providers_store` directly.
- `_load_providers_from_yaml` in `agent/config.py` is the only reader.

---

## MANDATORY: Read every file in full before writing code

**Files to read in full:**
1. `utils/providers_store.py` (current)
2. `utils/agent_defs.py` (lines 480-620 — provider section)
3. `agent/config.py` (lines 140-260 — provider load + config init)
4. `ui/handlers/agent_builder_handler.py` (full)
5. `ui/handlers/settings_handler.py` (full)
6. `ui/handlers/auxilium_wizard_handler.py` (full)
7. `tests/test_agent_config_yaml_fallback.py` (full)
8. `tests/test_agent_builder_handler.py` (full)
9. `tests/test_agent_defs.py` (full)
10. `tests/test_bug_fixes.py` (full)

**Anchors (identifiers, not line numbers):**
- `save_provider` (in `utils/agent_defs.py`)
- `delete_provider` (in `utils/agent_defs.py`)
- `_get_agent_json_path` (in `utils/agent_defs.py`)
- `get_available_providers` (in `utils/agent_defs.py`)
- `_load_providers_from_yaml` (in `agent/config.py`)
- `AgentBuilderHandler.save_provider` and `AgentBuilderHandler.delete_provider` (in `ui/handlers/agent_builder_handler.py`)

---

## Sub-Phases

This phase has 4 sub-phases. Each is independently verifiable. Do them in order. Run the full test suite after each sub-phase.

### Sub-Phase 1a: Migrate `agent.json` `providers` → `providers.yaml` on startup

**File:** `utils/providers_store.py`

**What to do:**
Add a new function `migrate_from_agent_json() -> int` that:
1. Reads `agent.json` from `get_config_dir()`.
2. If the file exists, has a `providers` key, and the value is a non-empty dict:
   - For each entry in the `providers` dict, convert to a `ProviderConfig` and append to a list.
   - Call `load_providers()` to get the current YAML list.
   - Merge: for each migrated provider, if a provider with the same name exists in YAML, skip (YAML wins — it's the current source of truth). Otherwise, add it.
   - Call `save_providers(merged_list)`.
   - Log INFO: `migrated N providers from agent.json to providers.yaml`.
   - **Do NOT delete the `providers` key from `agent.json` in this function.** That happens in a separate one-shot operation (see 1b).
3. Returns the number of providers migrated.

**Function signature:**
```python
def migrate_from_agent_json() -> int:
    """One-time migration: copy agent.json providers → providers.yaml.
    Returns count of providers migrated. Safe to call multiple times."""
```

**Caller:** Add a call to `migrate_from_agent_json()` in `_ensure_kb_provider_entry()` (or `ensure_kb_provider()`) in the same file. Run it on startup, before `_ensure_kb_provider_entry()` does its work.

**Test:** Add to `tests/test_providers_store.py` (create if doesn't exist, or add to `test_agent_defs.py`):
- `test_migrate_from_empty_agent_json_returns_zero` — empty agent.json → returns 0
- `test_migrate_moves_providers_to_yaml` — agent.json with 2 providers, empty YAML → returns 2, YAML now has 2
- `test_migrate_skips_yaml_existing` — agent.json has "openai", YAML has "openai" → migrate returns 0, YAML still has 1
- `test_migrate_handles_missing_agent_json` — no file → returns 0, no error

### Sub-Phase 1b: One-shot remove `providers` key from `agent.json`

**File:** `utils/providers_store.py`

**What to do:**
Add a new function `remove_providers_from_agent_json() -> bool` that:
1. Reads `agent.json` from `get_config_dir()`.
2. If the file has a `providers` key:
   - Remove the key.
   - Write back atomically (use the same .tmp + chmod 0o600 pattern as `save_provider`).
   - Log INFO: `removed legacy providers key from agent.json`.
   - Return True.
3. If no `providers` key: return False (idempotent).

**Function signature:**
```python
def remove_providers_from_agent_json() -> bool:
    """One-shot: remove legacy providers key from agent.json.
    Idempotent. Returns True if removed, False if not present."""
```

**Caller:** Add a call to `remove_providers_from_agent_json()` in `migrate_from_agent_json()` AFTER the migration succeeds. The order is: migrate providers to YAML, then strip the `providers` key from `agent.json`. This way, if the user runs the app once, all legacy data is consolidated.

**Test:** Add to same test file:
- `test_remove_providers_key_deletes_it` — agent.json with providers → returns True, file no longer has providers key, other keys preserved
- `test_remove_providers_key_idempotent` — agent.json without providers → returns False, no error

### Sub-Phase 1c: Remove `save_provider` and `delete_provider` from `utils/agent_defs.py`

**File:** `utils/agent_defs.py`

**What to do:**
1. Delete the `save_provider` function (currently around line 525-578).
2. Delete the `delete_provider` function (currently around line 581+).
3. Delete `_get_agent_json_path` if it's no longer used (check first).
4. Update `get_available_providers` if it needs adjustment — it already calls `load_providers`, so it should still work. Verify.

**Caller updates:**
- `ui/handlers/agent_builder_handler.py` has `save_provider` and `delete_provider` methods that wrap `utils.agent_defs.save_provider` and `delete_provider`. These need to be updated to call `utils.providers_store` directly:
  - `add_provider(provider: ProviderConfig)` → calls `from utils.providers_store import add_provider; providers = load_providers(); add_provider(providers, provider)`
  - `remove_provider(name: str)` → calls `from utils.providers_store import load_providers, remove_provider; providers = load_providers(); remove_provider(providers, name)`

**Test:** Update `tests/test_agent_builder_handler.py`:
- The existing test for `save_provider` should still pass (it tests the handler method, not the deleted function).
- The existing test for `delete_provider` should still pass.
- Add a new test: `test_agent_builder_writes_to_yaml_not_agent_json` — call `agent_builder_handler.save_provider(...)` and verify the provider is in `providers.yaml`, NOT in `agent.json`.

### Sub-Phase 1d: Update `agent/config.py:_load_providers_from_yaml` docstring

**File:** `agent/config.py`

**What to do:**
Update the docstring of `_load_providers_from_yaml` to reflect the new state:
- Remove the comment "If non-empty: convert each ProviderConfig → LLMProviderConfig" — keep it, but note "this is the SOLE source of provider config."
- The function itself does not change (it already only reads from `providers.yaml`).

Also verify the comment in `agent/config.py:300` (the "Match the format used by utils/providers_store.save_providers" comment) is still accurate.

**Test:** No new test — this is a docstring update. Run the existing test suite to confirm no regressions.

---

## Rules

1. Use the `steelFramedCodeWriter` prompt at `prompts/steelFramedCodeWriter.md` for ALL code changes.
2. Read every file in full before editing.
3. Anchor to identifiers, not line numbers.
4. Run the full test suite after each sub-phase:
   ```bash
   cd /path/to/projects/crabcakes
   python3 -m pytest tests/ -q --no-header --ignore=tests/test_kb_server.py --ignore=tests/test_runtime.py 2>&1 | tail -5
   ```
5. Report back with: files changed with line numbers, full test output, COMPLETENESS checklist per sub-phase, any issues found.
6. Do NOT touch the rest of `agent.json` — only the `providers` key.
7. Do NOT change the YAML format.
8. Do NOT change the `ProviderConfig` schema.
9. If you find a related bug in the same function, fix it now and report it. Do not silently fix unrelated bugs.

---

## Verification Commands (run after ALL sub-phases)

```bash
cd /path/to/projects/crabcakes
python3 -m pytest tests/ -q --no-header --ignore=tests/test_kb_server.py --ignore=tests/test_runtime.py 2>&1 | tail -5
python3 -c "
import os, json
# Confirm save_provider / delete_provider are gone
import utils.agent_defs
assert not hasattr(utils.agent_defs, 'save_provider'), 'save_provider should be deleted'
assert not hasattr(utils.agent_defs, 'delete_provider'), 'delete_provider should be deleted'
print('A-5 deletions: PASS')
"
grep -rn '"providers"' --include='*.py' utils/ agent/ ui/ 2>/dev/null | grep -v test_ | head -20
echo "--- (above shows ONLY the JSON key references that remain in non-test code; should be 0 or only in providers_store.py)"
```

---

## Report format

Send a completion report with:
- Sub-phases completed (1a, 1b, 1c, 1d)
- Files changed with line numbers
- Test output (full, not summary)
- COMPLETENESS checklist per sub-phase (all items `[x]` or `[NOT DONE] WHY`)
- Any related bugs found and fixed
- Any deviations from the spec, with one-sentence justification each

**Required word marker for /ask acknowledgment: "please write"**
