# PHASE 3 of 7 — handlers resolve prompts per-project

**Spec:** `docs/specs/SPEC-PROJECT-PROMPTS-DIRECTORY.md` (§2.3, §2.7(a)(b); §4 rows 4–5)
**Scope:** exactly 2 files — `ui/handlers/prompts_handler.py` and `ui/handlers/input_toolbar_handler.py`. NO window wiring yet (that is Phase 5).

## Supervisor notes (binding)

- Do NOT touch `self._favorites` semantics or `_sorted_filtered`'s `fpath in self._favorites` comparison — that line flips together with the favorites stem re-keying in Phase 4. This phase is resolution-only.
- The existing test fixture `tmp_prompts_dir` (tests/conftest.py:35) patches `PromptsHandler._get_prompts_dir` directly — it must keep passing unchanged.
- Handlers must not import other handlers (§8.6). Both already comply; keep it that way.

## Changes

### File 1: `ui/handlers/prompts_handler.py`

1. Delete the module-level constant at line ~20:
   `_PROMPTS_DIR = os.path.join(os.path.dirname(...), 'prompts')`
   Keep the `import os` (still used elsewhere).
2. Add import: `from utils.prompt_paths import get_project_prompts_dir`.
3. In `__init__`, add `self._project_path: str | None = None` with a short comment ("set via set_project_path(); None = app-level fallback").
4. Add public setter:

```python
def set_project_path(self, project_path: str | None) -> None:
    """Update the active project path ('' or None resets to app fallback).

    Caller is expected to trigger load_prompts()/refresh after calling.
    """
    self._project_path = project_path or None
```

5. Replace `_get_prompts_dir` body:

```python
def _get_prompts_dir(self) -> str:
    """Prompts dir for the active project; app-level fallback when no
    project is wired or the project has no .crabcakes/prompts/ yet."""
    return get_project_prompts_dir(self._project_path)
```

### File 2: `ui/handlers/input_toolbar_handler.py`

1. In `__init__`, add `self._project_path: str | None = None` + the same `set_project_path` setter (identical contract).
2. `load_prompt` (~line 450) and `save_as_prompt` (~line 405): replace both
   `from utils.prompts import PROMPTS_DIR` lazy imports +
   `os.path.join(PROMPTS_DIR, ...)` with:

```python
from utils.prompt_paths import get_project_prompts_dir   # local import, matching file style
prompts_dir = get_project_prompts_dir(self._project_path)
```

   (`save_as_prompt` already makedirs the dir afterward — keep that.)
3. Do NOT remove `PROMPTS_DIR` from utils/prompts.py — other callers exist; that file is Phase 5+ scope per spec §2.6.

## Tests (NEW file: `tests/test_prompts_project_resolution.py`)

No GTK needed — construct handlers directly (existing tests do this; follow tests/test_prompts_handler.py conventions):

1. `PromptsHandler()` fresh → `_get_prompts_dir()` == app-level dir (`from utils.prompt_paths import APP_USER_PROMPTS_DIR`).
2. `set_project_path(p)` where `<p>/.crabcakes/prompts/` exists (build with tmp_path) → returns the PROJECT dir; `load_prompts()` lists files from there (put a uniquely-named .md in the project dir and assert its name appears; put a different one in a fake app dir if you patch — prefer NOT patching: use real APP_USER_PROMPTS_DIR for the fallback side).
3. `set_project_path("")` then `set_project_path(None)` → back to app-level dir.
4. Unseeded project (no `.crabcakes/`) → still app-level fallback.
5. `InputToolbarHandler.load_prompt("name")` monkeypatched-free path check: build handler (pass main_content=None / GLib_module=None like existing tests do — read how tests/test_input_toolbar* or similar construct it; if construction requires GTK widgets, instead verify via reading the source + a targeted unit double: patch the instance's `load_file` to capture the path argument, set `_project_path` to a tmp project with `.crabcakes/prompts/foo.md`, call `load_prompt("foo")`, assert captured path == `<project>/.crabcakes/prompts/foo.md`).
6. Same for `save_as_prompt`: patch `self._mc.user_input.get_buffer` chain OR verify resolution by asserting the created file lands under `<project>/.crabcakes/prompts/` — use whatever stubbing pattern existing input-toolbar tests use; if full stubbing is disproportionate, cover save_as_prompt via source inspection assertion (grep-level test is NOT acceptable — instead instantiate with the same fakes existing tests use).

If InputToolbarHandler cannot be instantiated without GTK in this sandbox, document that limitation in the report and cover what is coverable headlessly (setter + resolution function calls on the instance via `InputToolbarHandler.__new__` bypassing __init__ IS acceptable — state it explicitly in COMPLETENESS).

Also: existing suites MUST stay green — `tests/test_prompts_handler.py`, `tests/test_chat_input_toolbar.py` (if present), `tests/test_favorites.py`.

## Rules

- Use the **steelFramedCodeWriter** prompt at `prompts/steelFramedCodeWriter.md`.
- Read both handler files fully before editing.
- No other files touched. No window.py changes.

## Verification (paste full output)

```bash
cd /path/to/projects/crabcakes && python3 -m pytest tests/test_prompts_project_resolution.py -v -p no:cacheprovider
cd /path/to/projects/crabcakes && python3 -m pytest tests/test_prompts_handler.py tests/test_favorites.py tests/test_prompt_loader.py tests/test_seed_project_prompts.py tests/test_prompt_paths.py -q -p no:cacheprovider
grep -n "_PROMPTS_DIR" ui/handlers/prompts_handler.py   # expect: no matches
```

COMPLETENESS:
- [x/not done] Edit 1: prompts_handler — constant deleted, setter + resolver-based _get_prompts_dir — evidence
- [x/not done] Edit 2: input_toolbar_handler — setter + two methods resolve per-project — evidence
- [x/not done] Tests 1–6 written and passing — full pytest output
