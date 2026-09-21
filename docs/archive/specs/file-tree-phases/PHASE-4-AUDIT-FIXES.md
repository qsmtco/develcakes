# Phase 4 — Audit Fixes (8 bugs)

**Source:** Debugger final audit (26 bugs total). Fixing the 8 that are real
production risks or trivial cleanup. Deferring cosmetic items (docstrings,
type hints, logging, BOM, path traversal).

---

## Fix 1 — BUG #13 (CRITICAL): status_porcelain fails on subdirs of git repos

**File:** `utils/git_ops.py` — `status_porcelain`.

**Bug:** `gitpython.Repo(project_path)` raises `InvalidGitRepositoryError` when
`project_path` is a subdirectory of a git repo (not the repo root). The bare
`except` catches it and returns `{}`. A project opened as a subdir shows no
git status at all.

**Fix:** Use `subprocess` to call `git status --porcelain` with `cwd=project_path`.
Git correctly walks up to find `.git`. Then normalize paths relative to the
project path (not the repo root), since the CLI output uses repo-root-relative paths.

```python
def status_porcelain(project_path: str) -> dict[str, str]:
    """Returns parsed git status map: {rel_path: status_code}.

    Handles subdirectories of git repos (walks up to find .git).
    Paths are normalized relative to project_path.
    Returns empty dict on any error.
    """
    import subprocess
    try:
        result = subprocess.run(
            ['git', 'status', '--porcelain'],
            cwd=project_path,
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return {}
        raw = result.stdout
        status_map: dict[str, str] = {}
        for line in raw.splitlines():
            if len(line) < 4:
                continue
            status_code = line[:2]
            rest = line[3:]
            if (status_code[0] in ('R', 'C') or
                (len(status_code) >= 2 and status_code[1] in ('R', 'C'))):
                if ' -> ' in rest:
                    rest = rest.split(' -> ', 1)[1]
            # Normalize: strip trailing slash on dirs
            rest = rest.rstrip('/')
            status_map[rest] = status_code
        return status_map
    except Exception:
        return {}
```

**Test:** Add to `tests/test_git_ops.py` — `TestStatusPorcelainFn`:
```python
    def test_subdirectory_of_git_repo(self, tmp_path):
        """status_porcelain works when project_path is a subdir of a git repo (BUG #13)."""
        import subprocess
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        subprocess.run(['git', 'init', '-q', str(repo_root)], check=True)
        subprocess.run(['git', '-C', str(repo_root), 'config', 'user.email', 't@t.com'], check=True)
        subprocess.run(['git', '-C', str(repo_root), 'config', 'user.name', 'T'], check=True)
        subdir = repo_root / "frontend"
        subdir.mkdir()
        (subdir / "app.py").write_text("x")
        result = status_porcelain(str(subdir))
        assert len(result) > 0, f"subdir returned empty: {result}"
```

## Fix 2 — BUG #14 (CRITICAL): _save_prefs crashes on PermissionError

**File:** `ui/handlers/file_tree_handler.py` — `_save_prefs`.

**Fix:** Wrap in try/except. Silently fail (don't crash the UI).

```python
    def _save_prefs(self) -> None:
        """Save sort mode to per-project prefs file."""
        if not self._prefs_path:
            return
        try:
            os.makedirs(os.path.dirname(self._prefs_path), exist_ok=True)
            with open(self._prefs_path, "w") as f:
                json.dump({"sort_mode": self._sort_mode}, f)
        except (OSError, IOError):
            pass  # Non-fatal — prefs are best-effort
```

## Fix 3 — BUG #15 (HIGH): Cache returned by reference

**File:** `ui/handlers/file_tree_handler.py` — `refresh_git_status`.

**Fix:** Return a copy:

```python
    def refresh_git_status(self) -> dict[str, str]:
        if not self._git_status_dirty:
            return dict(self._git_status_cache)
        self._git_status_cache = status_porcelain(self._project_path)
        self._git_status_dirty = False
        return dict(self._git_status_cache)
```

**Test:**
```python
    def test_cache_not_mutated_by_caller(self, tmp_path):
        """refresh_git_status returns a copy, not the internal cache (BUG #15)."""
        h = FileTreeHandler(str(tmp_path))
        r1 = h.refresh_git_status()
        r1["injected.py"] = "??"
        r2 = h.refresh_git_status()
        assert "injected.py" not in r2, "caller mutated internal cache"
```

## Fix 4 — BUG #16 (HIGH): None return from status_porcelain crashes

**File:** `ui/handlers/file_tree_handler.py` — `refresh_git_status`.

**Fix:** Validate the return:

```python
    def refresh_git_status(self) -> dict[str, str]:
        if not self._git_status_dirty:
            return dict(self._git_status_cache)
        result = status_porcelain(self._project_path)
        if not isinstance(result, dict):
            result = {}
        self._git_status_cache = result
        self._git_status_dirty = False
        return dict(self._git_status_cache)
```

## Fix 5 — BUG #18 (MEDIUM): Unhashable type crashes set_sort_mode

**File:** `ui/handlers/file_tree_handler.py` — `set_sort_mode`.

**Fix:** Add isinstance check:

```python
    def set_sort_mode(self, mode: str) -> None:
        if not isinstance(mode, str) or mode not in self._VALID_SORT_MODES:
            return
        if mode != self._sort_mode:
            self._sort_mode = mode
            self._save_prefs()
```

**Test:**
```python
    def test_set_sort_mode_unhashable_type(self):
        h = FileTreeHandler()
        h.set_sort_mode([])   # should not raise
        h.set_sort_mode({})   # should not raise
        assert h.get_sort_mode() == "name_asc"
```

## Fix 6 — BUG #26 (LOW): Cache not cleared on project switch

**File:** `ui/handlers/file_tree_handler.py` — `set_project_path`.

**Fix:** Clear the cache dict:

```python
    def set_project_path(self, path: str) -> None:
        self._project_path = path
        self._git_status_cache = {}
        self.invalidate_git_status()
        ...
```

## Fix 7 — BUG #17 (trivial): Remove dead MagicMock import

**File:** `tests/test_file_tree_handler.py`.

**Fix:** Change `from unittest.mock import patch, MagicMock` to `from unittest.mock import patch`.

## Fix 8 — BUG #20 (MEDIUM): Deleted project dir crashes save

Already handled by Fix 2 (the try/except in `_save_prefs` catches `FileNotFoundError`).

---

## Verification

```bash
cd /path/to/projects/crabcakes

# 1. Subdir-of-repo works
python3 << 'PYEOF'
import subprocess, os, tempfile
from utils.git_ops import status_porcelain
with tempfile.TemporaryDirectory() as d:
    subprocess.run(['git','init','-q',d], check=True)
    subprocess.run(['git','-C',d,'config','user.email','t@t.com'], check=True)
    subprocess.run(['git','-C',d,'config','user.name','T'], check=True)
    subdir = os.path.join(d, 'frontend')
    os.makedirs(subdir)
    with open(os.path.join(subdir, 'app.py'), 'w') as f: f.write('x')
    result = status_porcelain(subdir)
    assert len(result) > 0, f"subdir returned empty: {result}"
    print("SUBDIR OK:", result)
PYEOF

# 2. _save_prefs has try/except
grep -A2 "except" ui/handlers/file_tree_handler.py | head -4

# 3. Cache returns copy
grep "return dict" ui/handlers/file_tree_handler.py

# 4. isinstance check in set_sort_mode
grep "isinstance" ui/handlers/file_tree_handler.py

# 5. Cache cleared on project switch
grep "_git_status_cache = {}" ui/handlers/file_tree_handler.py

# 6. No dead import
grep "MagicMock" tests/test_file_tree_handler.py

# 7. Full suite
python3 -m pytest tests/test_file_icons.py tests/test_projects.py tests/test_git_ops.py tests/test_file_tree_helpers.py tests/test_file_tree_sort_filter.py tests/test_file_tree_handler.py -q
```

## Report back with

1. `git diff --stat`
2. Output of all 7 verification commands
3. COMPLETENESS checklist (Fixes 1–8)
