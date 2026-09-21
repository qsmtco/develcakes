# PHASE 3 FIX-1 — audit fixes for `save_as_prompt` + test gaps

**Origin:** Debugger adversarial audit of Phase 3 (verdict SEND-BACK). Supervisor verified findings.
**Scope:** exactly 2 files — `ui/handlers/input_toolbar_handler.py` and `tests/test_prompts_project_resolution.py`. Nothing else.

**Process note for the record:** the Phase 3 instructions explicitly said "`save_as_prompt` already makedirs the dir afterward — keep that." The line was deleted without being flagged in your report. Deviations from explicit delegation instructions MUST be flagged ("related issue found / deviation taken") even when they look like improvements.

## Required fix

### FIX 1 (BUG #1, MEDIUM) — restore makedirs in `save_as_prompt`

In `ui/handlers/input_toolbar_handler.py`, `save_as_prompt` (~line 411): after resolving
`prompts_dir = get_project_prompts_dir(self._project_path)` and BEFORE building `path`,
restore the master-spec line (spec §2.7b):

```python
        os.makedirs(prompts_dir, exist_ok=True)
```

Rationale (why the earlier removal was wrong): the resolver's clause-3 fallback returns the
APP dir for an unseeded project — correct for READ, wrong for WRITE. With makedirs restored,
a user saving a prompt into an unseeded project creates the PROJECT dir, so the write lands
in the project and every later read resolves there too. Matches `import_prompt`'s existing
behavior (same pattern, prompts_handler.py).

## Test additions/edits (all three)

### T1 (BUG #2) — strengthen existing test
In `test_set_project_path_empty_then_none_resets` (PromptsHandler): after EACH of
`set_project_path("")` and `set_project_path(None)`, also assert
`h._project_path is None`. Without this, a regression dropping the `or None`
still passes because the resolver masks it (mutation M3 missed).

### T2 (BUG #3) — new test: unseeded-project write
```python
def test_save_as_prompt_creates_unseeded_project_dir(...):
    # project tmp_path WITHOUT .crabcakes/ at all
    # handler.set_project_path(str(project))
    # stub buffer via the existing make_mock_buffer-style helper
    # result = handler.save_as_prompt("unseeded")
    # assert result == <project>/.crabcakes/prompts/unseeded.md
    # assert file exists there with the buffered text
```
This FAILS before FIX 1 (file lands in APP dir) and passes after. Run it both ways if
practical; at minimum paste the post-fix pass output.

### T3 (BUG #4) — stop writing to the real APP dir
In `test_save_as_prompt_empty_project_path_uses_app_dir`: monkeypatch
`utils.prompt_paths.APP_USER_PROMPTS_DIR` to a tmp_path subdir for the duration of the
test so the fallback write never touches the real repo prompts/ dir (no cleanup race).
Adjust assertions to the patched constant.

## Rules

- Use the **steelFramedCodeWriter** prompt at `prompts/steelFramedCodeWriter.md`.
- Read both files fully before editing.
- No other changes. Do not touch `load_prompt`.

## Verification (paste full output)

```bash
cd /path/to/projects/crabcakes && python3 -m pytest tests/test_prompts_project_resolution.py -v -p no:cacheprovider
cd /path/to/projects/crabcakes && python3 -m pytest tests/test_input_toolbar_handler.py tests/test_prompts_handler.py -q -p no:cacheprovider
grep -n "makedirs" ui/handlers/input_toolbar_handler.py
```

COMPLETENESS:
- [x/not done] Fix 1: makedirs restored in save_as_prompt — evidence (grep)
- [x/not done] T1: internal-state assertions added — evidence
- [x/not done] T2: unseeded-write test added and passing — full pytest output
- [x/not done] T3: real-FS write eliminated — evidence (patched constant)
