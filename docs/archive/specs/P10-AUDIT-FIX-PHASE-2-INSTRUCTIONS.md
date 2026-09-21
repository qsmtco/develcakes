# P10 Audit Fix — Phase 2: dead code, duplicate files, grep path normalization

**Bugs fixed:** BUG #1 (MEDIUM dead code), BUG #4 (MEDIUM duplicate files), BUG #8 (LOW grep path validation)
**File to change:** `agent/tools.py`
**Spec reference:** `docs/audits/2026-06-27-P10-ADVERSARIAL-AUDIT.md` BUG #1, #4, #8

## BUG #1 — Dead code after `_file_search` return (MEDIUM)

**Problem:** Lines ~641-650 of `agent/tools.py` contain unreachable code after an unconditional `return ToolResult(success=True, output=output)` at the end of `_file_search`. This dead block (`if returncode == 1: ... elif returncode != 0: ...`) is a leftover from when the function had inline grep. It can never execute.

**Fix:** Delete the dead block. The function ends at the `return ToolResult(success=True, output=output)` line.

### Current code (around line 638-650):
```python
    output = "\n".join(lines)
    if len(output) > MAX_EXEC_OUTPUT:
        output = output[:MAX_EXEC_OUTPUT] + f"\n[... truncated ...]"
    return ToolResult(success=True, output=output)

    if returncode == 1:
        return ToolResult(success=True, output="(no matches)")
    elif returncode != 0:
        return ToolResult(success=False, error=f"grep exited with {returncode}: {stderr[:200]}")

    output = stdout
    if len(output) > MAX_EXEC_OUTPUT:
        output = output[:MAX_EXEC_OUTPUT] + f"\n[... truncated ...]"
    return ToolResult(success=True, output=output)
```

### Replace with:
```python
    output = "\n".join(lines)
    if len(output) > MAX_EXEC_OUTPUT:
        output = output[:MAX_EXEC_OUTPUT] + f"\n[... truncated ...]"
    return ToolResult(success=True, output=output)
```

(Delete everything after the first `return ToolResult(...)` — all lines from `if returncode == 1:` through the final `return ToolResult(...)`.)

## BUG #4 — Duplicate files in `_file_search` output (MEDIUM)

**Problem:** `_file_search` merges filename matches from `_find_matching_files()` (which returns paths like `agent/tools.py`) with grep matches from `_run_grep()` (which returns paths like `./agent/tools.py`). The `set()` merge treats these as different files — the same file appears twice in the output.

**Root cause:** grep is run with `cwd=search_root` and pattern `"."`, so all output paths start with `./`. `_find_matching_files` returns relative paths without the `./` prefix.

**Fix:** Normalize grep output paths by stripping the `./` prefix when parsing grep results.

### Current code in `_file_search` (around line 585-595):
```python
    grep_hits: dict[str, list[tuple[int, str]]] = {}
    try:
        returncode, stdout, stderr = _run_grep(query, search_root, file_type)
        if returncode == 0 and stdout:
            for line in stdout.splitlines():
                # Parse grep output: file:line:content
                parts = line.split(":", 2)
                if len(parts) >= 3:
                    fpath, lineno, content = parts[0], int(parts[1]), parts[2]
                    if fpath not in grep_hits:
                        grep_hits[fpath] = []
                    grep_hits[fpath].append((lineno, content))
```

### Replace with:
```python
    grep_hits: dict[str, list[tuple[int, str]]] = {}
    try:
        returncode, stdout, stderr = _run_grep(query, search_root, file_type)
        if returncode == 0 and stdout:
            for line in stdout.splitlines():
                # Parse grep output: file:line:content
                parts = line.split(":", 2)
                if len(parts) >= 3:
                    fpath, lineno, content = parts[0], int(parts[1]), parts[2]
                    # Normalize grep paths: strip leading "./" so they match
                    # the format from _find_matching_files (relative, no prefix)
                    if fpath.startswith("./"):
                        fpath = fpath[2:]
                    if fpath not in grep_hits:
                        grep_hits[fpath] = []
                    grep_hits[fpath].append((lineno, content))
```

## BUG #8 — `_run_grep` missing `search_root` validation (LOW)

**Problem:** `_run_grep` accepts any string as `search_root` and passes it to `subprocess.run(cwd=search_root)`. While both callers validate the path first, the function itself should be defensive.

**Fix:** Add a guard at the top of `_run_grep` that rejects empty/None search_root.

### Current code (around line 506-510):
```python
def _run_grep(
    pattern: str,
    search_root: str,
    file_type: str | None = None,
    timeout: int = 10,
) -> tuple[int, str, str]:
    """Run grep and return (returncode, stdout, stderr).
    ...
    """
    cmd = ["grep", "-n", "-H", "--directories=skip", "-r"]
```

### Replace with:
```python
def _run_grep(
    pattern: str,
    search_root: str,
    file_type: str | None = None,
    timeout: int = 10,
) -> tuple[int, str, str]:
    """Run grep and return (returncode, stdout, stderr).

    Shared by _search_files (tool) and _file_search (tool) to guarantee
    identical grep behavior: same flags (-n -H --directories=skip -r),
    same --include filter, same -- separator, same timeout.
    """
    if not search_root or not isinstance(search_root, str):
        raise ValueError(f"search_root must be a non-empty string, got {search_root!r}")
    cmd = ["grep", "-n", "-H", "--directories=skip", "-r"]
```

## Verification

After edits, run:
```bash
# BUG #1: verify dead code is gone
python3 -c "
import ast, inspect
from agent.tools import _file_search
src = inspect.getsource(_file_search)
# Count 'return ToolResult' — should be exactly 3 (empty query, no matches, final output)
count = src.count('return ToolResult')
assert count == 3, f'Expected 3 returns, found {count}'
print(f'BUG #1: PASS ({count} returns)')

# Verify no 'returncode' references after the final return
lines = src.splitlines()
last_return_idx = max(i for i, l in enumerate(lines) if 'return ToolResult' in l)
trailing = '\\n'.join(lines[last_return_idx+1:])
assert 'returncode' not in trailing, f'Dead code after return: {trailing}'
print('BUG #1: no dead code after final return')
"

# BUG #4: verify path normalization
python3 -c "
import inspect
from agent.tools import _file_search
src = inspect.getsource(_file_search)
assert 'fpath.startswith(\"./\")' in src, 'Path normalization missing'
print('BUG #4: PASS (normalization code present)')
"

# BUG #8: verify guard
python3 -c "
import inspect
from agent.tools import _run_grep
src = inspect.getsource(_run_grep)
assert 'search_root must be a non-empty string' in src, 'Guard missing'
print('BUG #8: PASS (guard present)')
"

# Full test suite
python3 -m pytest tests/test_jit_context_discovery.py tests/test_tools.py -v --tb=short
```

## COMPLETENESS checklist (required in builder response):

```
COMPLETENESS:
- [ ] BUG #1: dead code deleted — evidence (paste last 5 lines of _file_search source)
- [ ] BUG #4: path normalization added — evidence (paste the normalization lines)
- [ ] BUG #8: search_root guard added — evidence (paste the guard lines)
- [ ] All tests pass — paste pytest output tail
```
