# PHASE 2 FIX-1 — audit fixes for `seed_project_prompts` + test gaps

**Origin:** Debugger adversarial audit of Phase 2 (verdict SEND-BACK). All findings supervisor-verified against source.
**Scope:** exactly 2 files — `utils/project_awareness.py` and `tests/test_seed_project_prompts.py`. Nothing else.

## Required fixes (blockers)

### FIX 1 (BUG #1, HIGH) — wrap top-level listdir

In `seed_project_prompts`, the top-level loop `for fname in os.listdir(APP_USER_PROMPTS_DIR):` has NO try/except (the subdir block does). A permission error or TOCTOU deletion between the `isdir` check and the `listdir` raises an uncaught exception. Wrap it, matching the subdir block's pattern:

```python
    # Top-level .md files
    try:
        entries = os.listdir(APP_USER_PROMPTS_DIR)
    except OSError as e:
        _logger.warning(
            "seed_project_prompts: cannot list %s: %s", APP_USER_PROMPTS_DIR, e
        )
        return False
    for fname in entries:
        ...  # body unchanged
```

(List once into a variable before iterating — do not call listdir inside the except-wrapped for-header.)

### FIX 2 (BUG #2, HIGH) — guard falsy project_path

Add at the very top of `seed_project_prompts`, BEFORE the source-dir check:

```python
    if not project_path:
        _logger.warning("seed_project_prompts: empty project_path")
        return False
```

Why: `os.path.join("", ".crabcakes", "prompts")` is the RELATIVE path `.crabcakes/prompts`; makedirs would create it in the process cwd. Empty string must be a hard failure returning False. Update the docstring's Returns line to mention "empty project_path".

## Recommended fix (same pass)

### FIX 3 (BUG #3, MEDIUM) — tighten copy-only-if-missing

`os.path.exists(dst)` is True for directories too, so a directory sitting at a `.md` name permanently blocks that prompt from ever being seeded, silently. In BOTH the top-level loop and the subdir loop, replace:

```python
            if not os.path.exists(dst):
```

with the two-tier check:

```python
            if os.path.isfile(dst):
                pass  # local copy exists — preserved (copy-only-if-missing)
            elif os.path.exists(dst):
                _logger.warning("seed: skipping %s — dest exists but is not a file", dst)
            else:
                try:
                    shutil.copy2(src, dst)
                except OSError as e:
                    _logger.warning("seed: copy %s failed: %s", src, e)
```

(Keep behavior identical otherwise: return value still True when the seed ran; skip-with-warning is not a hard failure.)

## Test additions (all three)

Append to `tests/test_seed_project_prompts.py`, reusing its existing fixture style:

1. `test_listdir_oserror_returns_false` — monkeypatch `os.listdir` (as seen by `utils.project_awareness`) to raise OSError; assert seed returns False and does not raise. Note: after FIX 2, patch on a valid non-empty project path so the new guard doesn't short-circuit first.
2. `test_empty_string_returns_false` — `seed_project_prompts("")` returns False AND does not create `<cwd>/.crabcakes/`. Assert both. (Use `os.path.isdir(".crabcakes")` from the repo root carefully — prefer asserting via monkeypatched cwd: use `monkeypatch.chdir(tmp_path)` then assert `(tmp_path / ".crabcakes").exists()` is False.)
3. `test_dest_directory_at_file_name_skips_with_warning` — create a DIRECTORY named like a seeded file (e.g. `README.md/`) inside dest `.crabcakes/prompts/` before seeding; assert seed returns True, the directory still exists, and NO file was copied over/into it (`(dest/"README.md").isdir()` stays True).
4. `test_source_is_a_file_returns_false` — point APP_USER_PROMPTS_DIR at an existing FILE; assert False (closes mutation gap: isdir→exists).
5. `test_nested_subdir_not_recursed` — add `default_agents/nested/deep.md` to the fake app prompts fixture; after seed, assert `<dest>/default_agents/nested/` does NOT exist (closes os.walk mutation gap).
6. `test_top_level_dir_named_md_not_copied` — add a top-level DIRECTORY named `fakemd` in the fake app dir; assert `<dest>/fakemd` does NOT exist (closes isfile→exists mutation gap).

Also update the fake-app fixture if needed for tests 5–6 (additive only).

## Rules

- Use the **steelFramedCodeWriter** prompt at `prompts/steelFramedCodeWriter.md`.
- Read the current function + test file fully before editing (read-before-touch).
- No other files. Do not touch callers (none exist yet — wiring is later phases).

## Verification (paste full output)

```bash
cd /path/to/projects/crabcakes && python3 -m pytest tests/test_seed_project_prompts.py -v -p no:cacheprovider
cd /path/to/projects/crabcakes && python3 -m pytest tests/test_prompt_paths.py tests/test_project_awareness.py -q -p no:cacheprovider
```

COMPLETENESS:
- [x/not done] Fix 1: top-level listdir wrapped — evidence (grep of new try/except)
- [x/not done] Fix 2: falsy project_path guard — evidence
- [x/not done] Fix 3: two-tier dest check in both loops — evidence
- [x/not done] Tests 1–6 added and passing — full pytest output
