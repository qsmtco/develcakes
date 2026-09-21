# PHASE 2 of 7 — `seed_project_prompts()` in `utils/project_awareness.py`

**Spec:** `docs/specs/SPEC-PROJECT-PROMPTS-DIRECTORY.md` (§2.1, §4 row 2, §6 acceptance criteria 4–5)
**Scope:** exactly 2 files — one existing module gains ONE function + constants + import; one NEW test file. Nothing else.

## What to build

**File 1: `utils/project_awareness.py` — add `seed_project_prompts()`**

Read the file first (read-before-touch). Then add near the other public helpers:

1. New import at top: `from utils.prompt_paths import APP_USER_PROMPTS_DIR` (prompt_paths is dependency-free — no cycle).
2. Module-level constants:

```python
_USER_PROMPTS_SUBDIR = "prompts"
# Subdirs of <app>/prompts/ seeded into projects. system/ and claude-code-clean/
# are app-level (see utils.prompt_paths.APP_LEVEL_PROMPTS_SUBDIRS) and excluded.
_USER_PROMPTS_INCLUDE_SUBDIRS = ("default_agents",)
```

3. The function (spec §2.1 verbatim semantics — copy-only-if-missing):

```python
def seed_project_prompts(project_path: str) -> bool:
    """Copy the app's user-facing prompt library into <project>/.crabcakes/prompts/.

    Copy-only-if-missing: existing project files are NEVER overwritten, so a
    project that customized a prompt keeps its local copy (the project is
    effectively branched after first seed — intentional, SPEC §2.1).

    Returns True on success (including no-op when dir exists), False on hard
    failure (source missing, cannot create dest). Idempotent.
    """
```

Behavior contract:
- If `APP_USER_PROMPTS_DIR` is not a dir → log warning, return False.
- Dest = `os.path.join(get_crabcakes_dir(project_path), _USER_PROMPTS_SUBDIR)`. Create dest with `os.makedirs(..., exist_ok=True)`; on OSError → log warning, return False.
- Top level: for each entry in `os.listdir(APP_USER_PROMPTS_DIR)`, if it's a **file** ending `.md`, copy with `shutil.copy2` **only if dest doesn't exist**. Per-file copy failures are logged and skipped (non-fatal).
- Subdirs: for each name in `_USER_PROMPTS_INCLUDE_SUBDIRS`, if `<app>/prompts/<name>` is a dir, create `<dest>/<name>` (exist_ok=True) and copy its **files** (any extension? NO — same rule as top level: files only; keep the spec's `os.path.isfile(src)` check; do not recurse deeper than one level) with the same copy-only-if-missing rule.
- Return True at the end.
- All logging via the module's existing logger pattern (`logger.warning(...)`) — follow the file's current logging style.

Do NOT delete or overwrite anything in the destination. Do NOT touch `prompts/system/` or `prompts/claude-code-clean/`.

**File 2 (NEW): `tests/test_seed_project_prompts.py`**

Use `tmp_path` + `monkeypatch`. Patch `APP_USER_PROMPTS_DIR` as seen by the module under test: `monkeypatch.setattr(utils.project_awareness, 'APP_USER_PROMPTS_DIR', str(fake_app_prompts))` where you build a fake app prompts dir containing e.g. `README.md`, `codeWriter.md`, `default_agents/coder.yaml`, plus an app-level `system/` subdir and a non-.md file to prove exclusion.

Required cases:

1. Fresh project (no `.crabcakes/` yet): seeds `.crabcakes/prompts/` with the top-level .md files + `default_agents/`; returns True.
2. Idempotent: second call returns True, file count unchanged.
3. Local edit survives re-seed: modify a seeded file's content, re-seed, modification intact (mtime/content preserved — assert content, not mtime).
4. App-level `system/` subdir NOT copied; non-`.md` top-level file NOT copied.
5. Missing source dir (patch APP_USER_PROMPTS_DIR to `/no/such/dir`): returns False, no exception.
6. Unwritable dest (patch `os.makedirs` to raise OSError via monkeypatch on `utils.project_awareness.os.makedirs`): returns False, no exception.
7. Deleted dest file re-created: seed, delete one file, re-seed, it's back (copy-only-if-missing fires because dest missing).
8. Extra local file not in app set: preserved untouched.
9. Real-repo smoke test (no patching): `seed_project_prompts(str(tmp_path))` against the actual repo `APP_USER_PROMPTS_DIR` returns True and creates ≥1 `.md` in `<tmp>/.crabcakes/prompts/` (this repo has README.md etc.).

## Rules

- Use the **steelFramedCodeWriter** prompt at `prompts/steelFramedCodeWriter.md` — apply every rule.
- Read `utils/project_awareness.py` (esp. `get_crabcakes_dir` ~line 89 and logging style) and `utils/prompt_paths.py` before writing.
- Do NOT wire seed calls anywhere (project_handler/window wiring is Phase 5/6). This phase is the function + its tests only.
- Do NOT modify any file other than `utils/project_awareness.py` and the new test file.

## Verification (paste full output)

```bash
cd /path/to/projects/crabcakes && python3 -m pytest tests/test_seed_project_prompts.py -v -p no:cacheprovider
cd /path/to/projects/crabcakes && python3 -m pytest tests/test_prompt_paths.py -q -p no:cacheprovider
cd /path/to/projects/crabcakes && python3 - <<'EOF'
import tempfile, os
from utils.project_awareness import seed_project_prompts
with tempfile.TemporaryDirectory() as d:
    print("ret:", seed_project_prompts(d))
    print(sorted(os.listdir(os.path.join(d, ".crabcakes", "prompts")))[:6])
EOF
```

## Report back

Files changed with line counts, full pytest output, any issues flagged-not-fixed.

COMPLETENESS:
- [x/not done] Edit 1: seed_project_prompts added with copy-only-if-missing semantics — evidence
- [x/not done] Edit 2: tests/test_seed_project_prompts.py covers cases 1–9 — evidence
