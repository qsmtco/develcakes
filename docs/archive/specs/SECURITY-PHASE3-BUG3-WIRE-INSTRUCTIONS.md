# Security Phase 3 — Bug 3 Wire-Up Fix

**Supervisor:** Qaster
**Date:** 2026-06-19
**Scope:** Wire `user_id` through `load_agent_config()` so A-4 audit log actually has a value
**Builder:** QTR — use `prompts/steelFramedCodeWriter.md` for all code changes

---

## Background

Previous round added `user_id: str = ""` field to `AgentConfig` dataclass (`agent/config.py:83`). Audit found that `load_agent_config()` does NOT read `user_id` from the JSON config file, and `_create_default_config()` does NOT include `user_id` in the example dict.

**Net effect:** `getattr(self._config, "user_id", "")` in `agent/runtime.py:1679,1699,1774` will still always be `""` in production. The bug is hidden behind a working-when-empty fallback rather than a crash.

## Bug to Fix

**File 1:** `agent/config.py` — `load_agent_config()` function
**Problem:** `AgentConfig(...)` constructor call (around line 236-245) does NOT pass `user_id`
**Fix:** Add `user_id=raw.get("user_id", "")` to the `AgentConfig(...)` call

**File 2:** `agent/config.py` — `_create_default_config()` function
**Problem:** `example` dict (around line 256-268) does NOT include `user_id` key
**Fix:** Add `"user_id": ""` to the `example` dict

**File 3:** `agent/runtime.py` (optional, for end-to-end wiring)
**Context:** The runtime needs to know who the user is. If no user_id is configured, the audit log will still be empty — which is correct behavior. The field is now queryable from config; setting it is a separate concern.
**Decision:** Leave runtime wiring for a future round. The dataclass field + config file wiring is the minimum needed to make the audit log functional when a user_id is set.

## Test to Add

Add to `tests/test_agent_config_yaml_fallback.py`:

```python
def test_user_id_loaded_from_config(tmp_path):
    """A-4: user_id from agent.json should populate AgentConfig.user_id."""
    config_path = tmp_path / "agent.json"
    config_path.write_text(json.dumps({
        "default_provider": "local-kb",
        "default_model": "local-kb/local-kb",
        "user_id": "alice@example.com",
    }))
    # ... use existing _fix_permissions / chmod dance from the test module
    config = load_agent_config(str(config_path))
    assert config.user_id == "alice@example.com"
```

If the test module's existing fixtures don't make this easy, add a minimal test that:
1. Writes a temp agent.json with `user_id` set
2. Calls `load_agent_config(path)`
3. Asserts `result.user_id == "alice@example.com"`

## Rules

1. Use the steelFramedCodeWriter prompt at `prompts/steelFramedCodeWriter.md` for ALL code changes
2. READ ALL FILES BEFORE STARTING (especially `agent/config.py` in full and the test module you add to)
3. Anchor to identifiers, not line numbers
4. Run the full test suite after the fix:
   ```bash
   cd /path/to/projects/crabcakes
   python3 -m pytest tests/test_agent_config_yaml_fallback.py tests/test_runtime.py -q --no-header 2>&1 | tail -5
   python3 -m pytest tests/ -q --no-header --ignore=tests/test_kb_server.py 2>&1 | tail -5
   ```
5. Report back with:
   - Files changed with line numbers
   - Full test output (paste it)
   - COMPLETENESS checklist
   - Any issues found

## Verification Commands

After fix:
```bash
cd /path/to/projects/crabcakes
python3 -c "from agent.config import load_agent_config, AgentConfig; import tempfile, json, os; tmp=tempfile.mkdtemp(); p=os.path.join(tmp,'agent.json'); json.dump({'user_id':'alice','default_provider':'local-kb','default_model':'local-kb/local-kb'}, open(p,'w')); os.chmod(p, 0o600); c=load_agent_config(p); assert c.user_id == 'alice'; print('user_id wire works:', c.user_id)"
```
