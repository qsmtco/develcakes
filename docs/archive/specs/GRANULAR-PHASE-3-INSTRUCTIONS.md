# GRANULAR Phase 3 of 8 — Tests for Steps 1-2

**Spec:** `docs/specs/SPEC-AUTO-ACCEPT-GRANULAR-1.md` Step 3
**File to change:** `tests/test_feed_handler.py` (append only)
**Builder prompt:** `prompts/steelFramedCodeWriter.md`

## CRITICAL: Read ALL files before starting

Before writing ANY code, READ these files completely:
1. `tests/test_feed_handler.py` — the ENTIRE file you will edit (understand conventions, imports, fixtures)
2. `models/feed_card.py` lines 198-292 — the Phase 1 dataclasses you are testing
3. `utils/feed_store.py` lines 283-424 — the Phase 2 functions you are testing
4. `docs/specs/SPEC-AUTO-ACCEPT-GRANULAR-1.md` §2.1 and §2.2 — the spec
5. `prompts/steelFramedCodeWriter.md` — your standing orders

## Task

Append two new test classes to the END of `tests/test_feed_handler.py`:

### Class 1: `TestAutoAcceptPrefs`

Tests the Phase 1 dataclasses (`FileChangePref`, `ExecCommandPref`, `AutoAcceptPrefs`).

Required test methods:
1. `test_defaults_all_disabled` — fresh `AutoAcceptPrefs()` has `any_enabled() == False`, all four file-change types disabled, exec mode == "off"
2. `test_enable_file_change_type` — enable one type, `any_enabled()` → True, `is_file_type_enabled()` returns True for that type, False for others
3. `test_enable_exec_command` — set exec mode to "show", `any_enabled()` → True
4. `test_to_dict_round_trip` — create prefs with mixed state, `to_dict()` → `from_dict()` → verify all fields preserved
5. `test_to_dict_has_version_2` — `to_dict()["version"] == 2`
6. `test_from_dict_empty` — `from_dict({})` returns all defaults
7. `test_from_dict_missing_keys` — partial dict with only some file_changes types
8. `test_instance_isolation` — two `AutoAcceptPrefs()` instances don't share mutable state
9. `test_locked_agent_none` — fresh prefs, `locked_agent()` → None
10. `test_locked_agent_specific` — set one type to a specific agent, `locked_agent()` → that agent name
11. `test_locked_agent_first_author` — agent_scope = "first_author" should NOT count as locked
12. `test_locked_agent_all_agents` — agent_scope = "all_agents" should NOT count as locked
13. `test_snoozed_card_ids_default_empty` — fresh prefs, `snoozed_card_ids == []`
14. `test_snoozed_card_ids_from_dict_non_list` — `from_dict({"auto_accept": {"snoozed_card_ids": "notalist"}})` → snoozed_card_ids == []

### Class 2: `TestPrefsMigration`

Tests the Phase 2 functions (`_default_prefs`, `_migrate_v1_to_v2`, `_merge_v2_defaults`, `load_feed_prefs`).

Required test methods:
1. `test_default_prefs_is_v2` — `_default_prefs()["version"] == 2`, has all required nested keys
2. `test_default_prefs_independent_instances` — two calls return independent dicts
3. `test_migrate_v1_disabled` — v1 with `auto_accept_enabled: False` → all disabled, scope = first_author
4. `test_migrate_v1_enabled_no_agent` — v1 with `auto_accept_enabled: True, auto_accept_agent: None` → all enabled, scope = first_author
5. `test_migrate_v1_enabled_with_agent` — v1 with `auto_accept_enabled: True, auto_accept_agent: "claude"` → all enabled, scope = "claude"
6. `test_migrate_v1_empty_dict` — v1 `{}` → all disabled
7. `test_merge_v2_complete` — full v2 dict passes through unchanged
8. `test_merge_v2_partial_missing_file_changes` — v2 with only diff → others filled from defaults
9. `test_merge_v2_empty_auto_accept` — v2 with `auto_accept: {}` → all defaults
10. `test_merge_v2_auto_accept_none` — v2 with `auto_accept: None` → all defaults (the isinstance guard)
11. `test_merge_v2_wrong_types` — v2 with wrong types at every level → all defaults
12. `test_load_v1_file_migrates` — write v1 JSON file, `load_feed_prefs()` → v2 dict (use `tempfile.TemporaryDirectory`)
13. `test_load_v2_file_preserves` — write v2 JSON file, `load_feed_prefs()` → same data
14. `test_load_missing_file_returns_defaults` — no file → defaults
15. `test_load_corrupt_file_returns_defaults` — invalid JSON → defaults
16. `test_load_unknown_version_returns_defaults` — version 99 → defaults

### Import note

The test file already imports from `models.feed_card` and `utils.feed_store`. Check the existing imports at the top of the file. You may need to add:
```python
from models.feed_card import AutoAcceptPrefs, FileChangePref, ExecCommandPref
from utils.feed_store import _default_prefs, _migrate_v1_to_v2, _merge_v2_defaults
```
Add these to the existing import block — do NOT duplicate existing imports.

### DO NOT:
- Modify any existing test classes or tests
- Modify any source files
- Add tests for UI behavior (that's later phases)

## Verification

```bash
# Run the new tests
python3 -m pytest tests/test_feed_handler.py::TestAutoAcceptPrefs tests/test_feed_handler.py::TestPrefsMigration -v

# Run ALL tests to verify no regressions
python3 -m pytest tests/test_feed_handler.py -v

# Verify line count
wc -l tests/test_feed_handler.py
```

## COMPLETENESS checklist

```
COMPLETENESS:
- [x/not done] TestAutoAcceptPrefs class added with 14 test methods — evidence (pytest -v output)
- [x/not done] TestPrefsMigration class added with 16 test methods — evidence (pytest -v output)
- [x/not done] All new tests pass — evidence (pytest output)
- [x/not done] All existing tests pass — evidence (pytest output)
- [x/not done] No existing tests modified — evidence (git diff)
```
