# P2 Audit Fixes — 5 Debugger bugs

**Source:** Debugger consolidated audit (`docs/specs/file-tree-phases/PHASE-2-AUDIT-REQUEST.md`).

**BUG #1 (critical)** — Status column is dead in production (callback never wired).
**BUG #3** — Directory status keys mismatch (trailing slash).
**BUG #5** — format_mtime sub-second epoch guard.
**BUG #2** — cleanup() docstring lies.
**BUG #4** — Tree-mode search placeholder misleads.

---

## Fix 1 — BUG #1: Wire git status callback (CRITICAL)

The `_on_get_git_status` callback on FileTree is never set in production. The
entire Status column renders empty cells.

### Fix 1a — Add `get_git_status` to ProjectHandler

**File:** `ui/handlers/project_handler.py`

Add this method to the `ProjectHandler` class (near `get_active_project_path`):

```python
def get_git_status(self) -> dict[str, str]:
    """Return parsed git status for the active project path.

    Called by FileTree via the set_on_get_git_status callback to populate
    the Status column. Returns {} if no project is open or on any error.
    """
    from utils.git_ops import status_porcelain
    path = self._active_project_path
    if not path:
        return {}
    return status_porcelain(path)
```

### Fix 1b — Wire the callback in window.py

**File:** `ui/window.py` — at line ~423 (after `set_project_handler`), add:

```python
left_panel._file_tree.set_on_get_git_status(self._project_handler.get_git_status)
```

### Verify

```bash
grep -n "set_on_get_git_status" ui/window.py   # → 1 match
grep -n "def get_git_status" ui/handlers/project_handler.py  # → 1 match
```

---

## Fix 2 — BUG #3: Directory status keys mismatch

**File:** `utils/git_ops.py` — `status_porcelain` function.

Untracked directories appear as `?? newdir/` (trailing slash) in porcelain
output, but `_show_tree` computes `os.path.relpath()` which returns `newdir`
(no slash). The dict lookup misses for directories.

**Fix:** In `status_porcelain`, after extracting `rest` (the path), strip a
trailing slash for directories. The simplest correct fix is to `rstrip('/')`
the key before storing in the result dict:

In the loop, after computing `rest` (after the rename split), change:
```python
            result[rest] = status_code
```
to:
```python
            # Normalize: strip trailing slash so directory keys match
            # os.path.relpath() output (BUG #3 — inter-layer consistency)
            result[rest.rstrip('/')] = status_code
```

**Verify:**
```bash
python3 -c "
import subprocess, os, tempfile
from utils.git_ops import status_porcelain
with tempfile.TemporaryDirectory() as d:
    os.chdir(d)
    subprocess.run(['git','init','-q'], check=True)
    subprocess.run(['git','config','user.email','t@t.com'], check=True)
    subprocess.run(['git','config','user.name','T'], check=True)
    with open('root.txt','w') as f: f.write('x')
    subprocess.run(['git','add','-A'], check=True)
    subprocess.run(['git','commit','-qm','init'], check=True)
    os.makedirs('newdir')
    with open('newdir/inside.py','w') as f: f.write('y')
    result = status_porcelain(d)
    print('keys:', list(result.keys()))
    assert 'newdir' in result, f'newdir missing (got {list(result.keys())})'
    assert 'newdir/' not in result, 'trailing slash should be stripped'
    print('PASS')
"
```

---

## Fix 3 — BUG #5: format_mtime sub-second epoch

**File:** `ui/views/file_tree.py` — `format_mtime`.

`format_mtime(500_000_000)` returns "Dec 31" because `500_000_000 // 1_000_000_000 == 0` → `datetime.fromtimestamp(0)` = 1970.

**Fix:** Change the guard from `if mtime_ns <= 0:` to `if mtime_ns < 1_000_000_000:`:

```python
    if mtime_ns < 1_000_000_000:  # sub-second-since-epoch is invalid (BUG #5)
        return "—"
```

**Verify:**
```bash
python3 -c "
from ui.views.file_tree import format_mtime
assert format_mtime(500_000_000) == '—', format_mtime(500_000_000)
assert format_mtime(0) == '—'
assert format_mtime(1) == '—'
print('PASS')
"
```

Add this test case to `tests/test_file_tree_helpers.py` in the `format_mtime` test class:
```python
    def test_sub_second_epoch_returns_dash(self):
        assert format_mtime(500_000_000) == "—"
        assert format_mtime(1) == "—"
        assert format_mtime(999_999_999) == "—"
```

---

## Fix 4 — BUG #2: cleanup() docstring

**File:** `ui/views/file_tree.py:249`

**Fix:** Change the docstring to match what the method actually does:

```python
    def cleanup(self) -> None:
        """Detach drawer, clear bound row reference."""
```

(Do NOT add the disconnect — `_on_bind` handles that on rebind. Just fix the lie.)

---

## Fix 5 — BUG #4: Tree-mode search placeholder

**File:** `ui/views/file_tree.py` — `_show_tree` (the search placeholder line).

Current: `self._search_entry.set_placeholder_text("Search files... (Esc to clear)")`
This implies search works, but it's a no-op until Phase 3.

**Fix:** Change to a clear disabled indicator:

```python
        self._search_entry.set_placeholder_text("Search files...")
```

(Remove the "(Esc to clear)" — there's no Esc handler wired yet either. Phase 3
will restore the full placeholder when search goes live.)

---

## Verification (run ALL)

```bash
cd /path/to/projects/crabcakes

# 1. BUG #1 — callback wired
grep -n "set_on_get_git_status" ui/window.py
grep -n "def get_git_status" ui/handlers/project_handler.py

# 2. BUG #3 — directory keys normalized
python3 -c "
import subprocess, os, tempfile
from utils.git_ops import status_porcelain
with tempfile.TemporaryDirectory() as d:
    os.chdir(d); subprocess.run(['git','init','-q'],check=True)
    subprocess.run(['git','config','user.email','t@t.com'],check=True)
    subprocess.run(['git','config','user.name','T'],check=True)
    with open('r.txt','w') as f: f.write('x')
    subprocess.run(['git','add','-A'],check=True); subprocess.run(['git','commit','-qm','i'],check=True)
    os.makedirs('nd'); open(os.path.join(d,'nd','f.py'),'w').write('y')
    r = status_porcelain(d)
    assert 'nd' in r and 'nd/' not in r
    print('PASS')
"

# 3. BUG #5 — sub-second guard
python3 -c "
from ui.views.file_tree import format_mtime
assert format_mtime(500_000_000) == '—'
assert format_mtime(0) == '—'
print('PASS')
"

# 4. BUG #2 — docstring fixed
grep -A1 "def cleanup" ui/views/file_tree.py | grep -v "disconnect signals"

# 5. BUG #4 — placeholder updated
grep "Search files" ui/views/file_tree.py

# 6. Full test suite
python3 -m pytest tests/test_file_icons.py tests/test_projects.py tests/test_git_ops.py tests/test_file_tree_helpers.py -q
```

## Report back with

1. `git diff --stat`
2. Output of all 6 verification commands
3. COMPLETENESS checklist (Fixes 1–5)
