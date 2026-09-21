# PHASE 1 of 7 — `utils/prompt_paths.py` (NEW resolver module)

**Spec:** `docs/specs/SPEC-PROJECT-PROMPTS-DIRECTORY.md` (§2.2, §4 row 1, §6 acceptance criteria 1–3)
**Scope:** exactly 2 files — one NEW module, one NEW test file. Nothing else.

## What to build

**File 1 (NEW): `utils/prompt_paths.py`**

Pure-Python resolver module. No GTK, no I/O beyond `os.path`. Content:

```python
# utils/prompt_paths.py
# Path resolvers for the per-project prompts library.
#
# Architecture: pure Python, no GTK, no I/O beyond os.path.
# Consumed by ui/handlers/prompts_handler.py and utils/prompts.py so the
# resolution logic lives in ONE place (SPEC-PROJECT-PROMPTS-DIRECTORY §2.2).

import os


def _get_app_root() -> str:
    """Return the app install dir (crabcakes project root)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


APP_USER_PROMPTS_DIR: str = os.path.join(_get_app_root(), "prompts")

# Subdirectories of <app>/prompts/ that are APP-LEVEL (NOT seeded per project).
# system/ = agent personality templates consumed by utils/prompt_loader.py.
# claude-code-clean/ = third-party human reference material.
APP_LEVEL_PROMPTS_SUBDIRS: frozenset = frozenset({"system", "claude-code-clean"})


def get_project_prompts_dir(project_path: str | None) -> str:
    """Return the per-project prompts directory, or app-level fallback.

    Resolution order (SPEC §2.2):
      1. project_path None/empty -> APP_USER_PROMPTS_DIR (no project open)
      2. <project>/.crabcakes/prompts/ exists -> return it
      3. otherwise -> APP_USER_PROMPTS_DIR (unseeded project / legacy state)

    Never raises: falls back to APP_USER_PROMPTS_DIR on any os.path.isdir error.
    """
    if not project_path:
        return APP_USER_PROMPTS_DIR
    proj = os.path.join(project_path, ".crabcakes", "prompts")
    try:
        if os.path.isdir(proj):
            return proj
    except OSError:
        pass
    return APP_USER_PROMPTS_DIR
```

Adapt comments/style to codebase conventions as needed, but keep the public surface exactly: `APP_USER_PROMPTS_DIR`, `APP_LEVEL_PROMPTS_SUBDIRS`, `get_project_prompts_dir`. The empty-string case (`''`) MUST behave like `None`.

**File 2 (NEW): `tests/test_prompt_paths.py`**

One test file per module (`tests/test_<name>.py` convention). Cover at minimum — these map to spec §6 acceptance criteria:

1. `get_project_prompts_dir(None)` returns `APP_USER_PROMPTS_DIR`
2. `get_project_prompts_dir("")` returns `APP_USER_PROMPTS_DIR`
3. `get_project_prompts_dir("/no/such/path")` returns `APP_USER_PROMPTS_DIR`
4. tmp_path with `.crabcakes/prompts/` created → returns `<tmp>/.crabcakes/prompts`
5. same tmp_path WITHOUT `.crabcakes/prompts/` → returns `APP_USER_PROMPTS_DIR`
6. `os.path.isdir` raising OSError → returns `APP_USER_PROMPTS_DIR`, does NOT raise (use monkeypatch)
7. relative-path sanity: `APP_USER_PROMPTS_DIR` endswith `/prompts` and points at the repo's existing `prompts/` dir (i.e., `os.path.isdir(APP_USER_PROMPTS_DIR)` is True when run inside this repo)

Use `tmp_path` fixture, `monkeypatch` where patching is needed. Aim to break the code, not confirm it works (project test conventions).

## Rules

- Use the **steelFramedCodeWriter** prompt at `prompts/steelFramedCodeWriter.md` — apply every rule.
- Read `utils/config.py`, `utils/prompts.py`, `utils/prompt_loader.py`, `ui/handlers/prompts_handler.py` before writing (read-before-touch).
- Do NOT modify any other file. `prompts/system/*` and `utils/prompt_loader.SYSTEM_DIR` are out of scope (spec §9).
- Do NOT wire anything yet — consumers come in later phases. This phase is the foundation module only.

## Verification (paste full output)

```bash
cd /path/to/projects/crabcakes && python3 -m pytest tests/test_prompt_paths.py -v -p no:cacheprovider
cd /path/to/projects/crabcakes && python3 -c "from utils.prompt_paths import get_project_prompts_dir, APP_USER_PROMPTS_DIR; print(APP_USER_PROMPTS_DIR); print(get_project_prompts_dir(None)); print(get_project_prompts_dir('/nonexistent'))"
```

## Report back

- Files changed with line counts (`wc -l`)
- Full pytest output
- Any issues or related bugs found but NOT fixed (flag, don't fix silently)

At the end include a completeness checklist:

COMPLETENESS:
- [x/not done] Edit 1: utils/prompt_paths.py created with exact public surface — evidence
- [x/not done] Edit 2: tests/test_prompt_paths.py covers cases 1–7 — evidence
