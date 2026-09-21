# GRANULAR Phase 1 of 8 — AutoAcceptPrefs Dataclass

**Spec:** `docs/specs/SPEC-AUTO-ACCEPT-GRANULAR-1.md` §2.1
**File to change:** `models/feed_card.py` (append only — no existing code modified)
**Builder prompt:** `prompts/steelFramedCodeWriter.md`

## CRITICAL: Read ALL files before starting

Before writing ANY code, READ these files completely:
1. `models/feed_card.py` — the file you will edit (ALL of it)
2. `docs/specs/SPEC-AUTO-ACCEPT-GRANULAR-1.md` §2.1 — the spec section with the exact dataclass definitions
3. `utils/feed_store.py` lines 277-361 — the current v1 prefs code (so you understand what v1 looks like)
4. `prompts/steelFramedCodeWriter.md` — your standing orders

## Task

Append three new dataclasses to the END of `models/feed_card.py` (after the existing `FeedCardData` class and all its methods):

1. **`FileChangePref`** — per-type file-change auto-accept preference
2. **`ExecCommandPref`** — exec command auto-accept preference
3. **`AutoAcceptPrefs`** — top-level v2 auto-accept preferences container

The exact code for all three dataclasses is in spec §2.1. Copy it verbatim — every field, every method, every docstring. The spec is your contract.

### Key points from the spec:

- `FileChangePref` has two fields: `enabled: bool = False` and `agent_scope: str = "first_author"`
- `ExecCommandPref` has two fields: `mode: str = "off"` and `agent_scope: str = "first_author"`
- `AutoAcceptPrefs` has three fields: `file_changes` (dict of card_type → FileChangePref), `exec_command` (ExecCommandPref), `snoozed_card_ids` (list[str])
- `AutoAcceptPrefs` has methods: `any_enabled()`, `is_file_type_enabled(card_type)`, `to_dict()`, `from_dict(raw)` (staticmethod), `locked_agent()`
- Uses `field(default_factory=...)` from dataclasses — `field` is already imported in `models/feed_card.py` line 4

### DO NOT:
- Modify any existing code in `models/feed_card.py`
- Change imports (field is already imported)
- Add any GTK imports
- Rename any fields or methods

## Verification

After writing, run these commands and paste ALL output:

```bash
# Verify the dataclass imports and works
python3 -c "
from models.feed_card import AutoAcceptPrefs, FileChangePref, ExecCommandPref
p = AutoAcceptPrefs()
assert not p.any_enabled(), 'empty prefs should not be enabled'
p.file_changes['diff'].enabled = True
assert p.any_enabled(), 'diff enabled should make any_enabled True'
assert p.is_file_type_enabled('diff'), 'diff should be enabled'
assert not p.is_file_type_enabled('file_created'), 'file_created should not be enabled'
d = p.to_dict()
assert d['version'] == 2
p2 = AutoAcceptPrefs.from_dict(d)
assert p2.any_enabled()
assert p2.is_file_type_enabled('diff')
print('All checks passed')
"

# Verify line count
wc -l models/feed_card.py

# Verify no existing code was modified
git diff models/feed_card.py
```

## COMPLETENESS checklist

Report back with this format:
```
COMPLETENESS:
- [x/not done] FileChangePref dataclass added — evidence (line N)
- [x/not done] ExecCommandPref dataclass added — evidence (line N)
- [x/not done] AutoAcceptPrefs dataclass added — evidence (line N)
- [x/not done] any_enabled() works — evidence (test output)
- [x/not done] is_file_type_enabled() works — evidence (test output)
- [x/not done] to_dict()/from_dict() round-trip works — evidence (test output)
- [x/not done] No existing code modified — evidence (git diff output)
- [x/not done] locked_agent() method present — evidence (grep output)
```
