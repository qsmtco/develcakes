# PHASE 4 of 7 — favorites keyed by stem + one-time path migration

**Spec:** `docs/specs/SPEC-PROJECT-PROMPTS-DIRECTORY.md` (§2.8; §6 acceptance row 8; §7 cross-project favorite edge case)
**Scope:** exactly 3 files — `utils/favorites.py`, `ui/handlers/prompts_handler.py` (favorites-related lines only), `tests/test_favorites.py`. Nothing else.

## Why

After Phase 3, a prompt's filepath differs per project (`<app>/prompts/X.md` vs `<project>/.crabcakes/prompts/X.md`). Favorites keyed by absolute path silently un-favorite across project switches. Re-key to the STEM (`X`) so a favorite follows the prompt name everywhere.

## Changes

### File 1: `utils/favorites.py`

Rewrite keying to stems. Public API names and signatures stay IDENTICAL (`load_favorites() -> set[str]`, `save_favorites(set[str]) -> None`, `is_favorite(stem) -> bool`, `toggle_favorite(stem) -> bool`) — only the UNIT stored changes.

1. `load_favorites()` gains one-time idempotent migration: for each entry containing `/`, strip to basename without extension:
   ```python
   migrated = [
       os.path.splitext(os.path.basename(p))[0] if "/" in p else p
       for p in favs
   ]
   if migrated != favs:
       save_favorites(set(migrated))   # persist migrated form
   return set(migrated)
   ```
   Keep existing error behavior (missing file / bad JSON / non-list → empty set).
2. Docstrings updated: "favorite STEMS (not paths)". Module header comment updated.
3. Migration must NOT crash on weird entries (empty string stays empty string; entry like `"a/b/c.MD"` keeps case — do not lowercase).

### File 2: `ui/handlers/prompts_handler.py` — favorites lines ONLY

1. `_sorted_filtered`: replace
   ```python
   fpath = p['filepath']
   is_fav = fpath in self._favorites
   ```
   with stem comparison:
   ```python
   is_fav = p['name'] in self._favorites
   ```
   (`p['name']` is already the stem — `_scan_prompts` strips `.md`.) Keep `fpath` var ONLY if still needed for `last_used_str`; otherwise drop it. Do NOT touch last_used logic.
2. `toggle_favorite(self, filepath)` method: extract stem before delegating:
   ```python
   stem = os.path.splitext(os.path.basename(filepath))[0]
   is_now_fav = fav.toggle_favorite(stem)
   ```
3. `__init__` comment `# set of filepaths` → `# set of prompt name stems`.
4. `load_prompts()` docstring if it mentions filepaths for favorites — align wording.
5. NOTHING else changes in this file (no resolution changes — done in Phase 3).

### File 3: `tests/test_favorites.py`

Update existing tests to stem semantics AND add:

1. Migration: write a favorites.json containing `["/old/app/prompts/steelFramedCodeWriter.md", "/x/y/READMD.md"]`; load; assert set == `{"steelFramedCodeWriter", "READMD"}`; assert FILE was rewritten (reload raw JSON, entries contain no `/`).
2. Migration idempotent: second load does not rewrite (content identical after first).
3. Round-trip: `toggle_favorite("foo")` True → json contains `"foo"`; `toggle_favorite("foo")` False → gone.
4. Cross-project persistence (spec §7): two different dirs each containing `<stem>.md`; favoriting via handler in dir A shows starred when scanning dir B. Use `PromptsHandler` with `_get_prompts_dir` patched (tmp_prompts_dir pattern) OR call handler.toggle_favorite with paths in A then scan B — either acceptable, state which.
5. Malformed JSON → empty set, no raise (may already exist — keep).

## Rules

- Use the **steelFramedCodeWriter** prompt at `prompts/steelFramedCodeWriter.md`.
- Read all three files fully first. Flag any deviation EXPLICITLY (per Phase 3 process note).
- Existing suites that must stay green: `tests/test_prompts_handler.py`, `tests/test_prompts_project_resolution.py`.

## Verification (paste full output)

```bash
cd /path/to/projects/crabcakes && python3 -m pytest tests/test_favorites.py -v -p no:cacheprovider
cd /path/to/projects/crabcakes && python3 -m pytest tests/test_prompts_handler.py tests/test_prompts_project_resolution.py tests/test_prompt_paths.py -q -p no:cacheprovider
grep -n "is_fav" ui/handlers/prompts_handler.py
```

COMPLETENESS:
- [x/not done] Edit 1: favorites stem keying + migration — evidence
- [x/not done] Edit 2: handler stem comparison + stem toggle — evidence (grep)
- [x/not done] Tests 1–5 added/passing — full pytest output
