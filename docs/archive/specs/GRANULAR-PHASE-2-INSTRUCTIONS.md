# GRANULAR Phase 2 of 8 — Prefs v2 Schema + Migration

**Spec:** `docs/specs/SPEC-AUTO-ACCEPT-GRANULAR-1.md` §2.2
**File to change:** `utils/feed_store.py`
**Builder prompt:** `prompts/steelFramedCodeWriter.md`

## CRITICAL: Read ALL files before starting

Before writing ANY code, READ these files completely:
1. `utils/feed_store.py` — the ENTIRE file you will edit
2. `docs/specs/SPEC-AUTO-ACCEPT-GRANULAR-1.md` §2.2 — the spec section with exact code
3. `models/feed_card.py` — Phase 1 output (AutoAcceptPrefs dataclass, especially `to_dict()` and `from_dict()`)
4. `prompts/steelFramedCodeWriter.md` — your standing orders

## Task

Modify `utils/feed_store.py` per spec §2.2. There are exactly 4 changes:

### Change 1: Bump PREFS_VERSION (line 21)

Change `PREFS_VERSION = 1` to `PREFS_VERSION = 2`.

### Change 2: Replace `_default_prefs()` (currently lines 283-288)

Replace the current v1 default with the v2 default from the spec. The new version returns a dict with `"version": 2` and nested `"auto_accept"` containing `file_changes`, `exec_command`, and `snoozed_card_ids`.

Copy the code verbatim from spec §2.2 change #2.

### Change 3: Replace `load_feed_prefs()` (currently lines 292-339)

Replace the entire function with the new version from spec §2.2 change #3. The new version:
- Handles version 2 files via `_merge_v2_defaults(raw)`
- Handles version 1 files via `_migrate_v1_to_v2(raw)`
- Returns `_default_prefs()` for missing/invalid/unknown versions

Copy the code verbatim from spec §2.2 change #3.

### Change 4: Add two new helper functions

Add `_migrate_v1_to_v2(raw: dict) -> dict` and `_merge_v2_defaults(raw: dict) -> dict` as new module-level functions.

Place them AFTER `load_feed_prefs()` and BEFORE `save_feed_prefs()`.

Copy the code verbatim from spec §2.2 change #4.

### `save_feed_prefs()` — NO CODE CHANGE

The spec explicitly states: "No code change needed beyond the constant bump." The existing version check `prefs.get("version") != PREFS_VERSION` will now require version 2 automatically.

### DO NOT:
- Modify `save_feed_prefs()` logic
- Modify any other functions in the file
- Change imports
- Add tests (that's Phase 3)

## Verification

After writing, run these commands and paste ALL output:

```bash
# Verify PREFS_VERSION is 2
grep -n "PREFS_VERSION" utils/feed_store.py

# Verify all four functions exist
grep -n "def _default_prefs\|def load_feed_prefs\|def _migrate_v1_to_v2\|def _merge_v2_defaults\|def save_feed_prefs" utils/feed_store.py

# Verify v1 migration works
python3 -c "
import tempfile, os, json
from utils.feed_store import load_feed_prefs, save_feed_prefs, _default_prefs, _migrate_v1_to_v2, _merge_v2_defaults

# Test 1: default is v2
d = _default_prefs()
assert d['version'] == 2
assert 'auto_accept' in d
assert 'file_changes' in d['auto_accept']
assert 'exec_command' in d['auto_accept']
assert 'snoozed_card_ids' in d['auto_accept']
print('Test 1 passed: default is v2')

# Test 2: v1 migration with enabled=False
v1 = {'version': 1, 'auto_accept_enabled': False, 'auto_accept_agent': None}
v2 = _migrate_v1_to_v2(v1)
assert v2['version'] == 2
for ct in ('diff', 'file_created', 'file_modified', 'file_deleted'):
    assert v2['auto_accept']['file_changes'][ct]['enabled'] == False
assert v2['auto_accept']['exec_command']['mode'] == 'off'
print('Test 2 passed: v1 disabled migration')

# Test 3: v1 migration with enabled=True, agent=None
v1 = {'version': 1, 'auto_accept_enabled': True, 'auto_accept_agent': None}
v2 = _migrate_v1_to_v2(v1)
for ct in ('diff', 'file_created', 'file_modified', 'file_deleted'):
    assert v2['auto_accept']['file_changes'][ct]['enabled'] == True
    assert v2['auto_accept']['file_changes'][ct]['agent_scope'] == 'first_author'
print('Test 3 passed: v1 enabled, no agent -> first_author')

# Test 4: v1 migration with enabled=True, agent='claude'
v1 = {'version': 1, 'auto_accept_enabled': True, 'auto_accept_agent': 'claude'}
v2 = _migrate_v1_to_v2(v1)
for ct in ('diff', 'file_created', 'file_modified', 'file_deleted'):
    assert v2['auto_accept']['file_changes'][ct]['agent_scope'] == 'claude'
print('Test 4 passed: v1 enabled with agent -> agent scope preserved')

# Test 5: merge v2 defaults fills missing keys
partial = {'version': 2, 'auto_accept': {'file_changes': {'diff': {'enabled': True}}}}
merged = _merge_v2_defaults(partial)
assert merged['auto_accept']['file_changes']['diff']['enabled'] == True
assert merged['auto_accept']['file_changes']['file_created']['enabled'] == False
assert merged['auto_accept']['exec_command']['mode'] == 'off'
print('Test 5 passed: merge fills defaults')

# Test 6: full round trip via file I/O
with tempfile.TemporaryDirectory() as tmpdir:
    os.makedirs(os.path.join(tmpdir, '.crabcakes'))
    # Write v1 file
    path = os.path.join(tmpdir, '.crabcakes', 'feed-prefs.json')
    with open(path, 'w') as f:
        json.dump({'version': 1, 'auto_accept_enabled': True, 'auto_accept_agent': None}, f)
    # Load - should auto-migrate
    loaded = load_feed_prefs(tmpdir)
    assert loaded['version'] == 2
    assert loaded['auto_accept']['file_changes']['diff']['enabled'] == True
    # Save back
    save_feed_prefs(tmpdir, loaded)
    # Reload - should be v2 now
    with open(path) as f:
        raw = json.load(f)
    assert raw['version'] == 2
    print('Test 6 passed: v1 file -> load -> save -> v2 file round trip')

print('All checks passed')
"

# Verify existing tests still pass
python3 -m pytest tests/test_feed_store.py -v

# Verify line count
wc -l utils/feed_store.py

# Verify diff is scoped to what we expect
git diff utils/feed_store.py
```

## COMPLETENESS checklist

Report back with:
```
COMPLETENESS:
- [x/not done] PREFS_VERSION bumped to 2 — evidence (grep output)
- [x/not done] _default_prefs() returns v2 dict — evidence (test output)
- [x/not done] load_feed_prefs() handles v1/v2/missing — evidence (test output)
- [x/not done] _migrate_v1_to_v2() added — evidence (grep output)
- [x/not done] _merge_v2_defaults() added — evidence (grep output)
- [x/not done] save_feed_prefs() unchanged except version constant — evidence (git diff)
- [x/not done] All 13 existing feed_store tests pass — evidence (pytest output)
- [x/not done] v1→v2 migration preserves agent lock-in — evidence (test 4 output)
- [x/not done] v1→v2 round trip via file I/O works — evidence (test 6 output)
```
