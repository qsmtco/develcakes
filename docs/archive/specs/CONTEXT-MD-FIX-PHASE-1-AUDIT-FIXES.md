# Phase 1 Audit Fixes — project_awareness.py

**Spec:** `docs/specs/SPEC-CONTEXT-MD-SYSTEM-FIX.md` (parent spec)
**Audit:** `docs/specs/CONTEXT-MD-FIX-PHASE-1-INSTRUCTIONS.md` Phase 1 + Debugger audit (6 bugs)
**File:** `utils/project_awareness.py` (fixes) + `tests/test_project_awareness.py` (new tests)

All 4 bugs confirmed by Supervisor's own reproduction. Use `steelFramedCodeWriter` (sent in the delegation message). Read this file in full. Read `utils/project_awareness.py` in full before editing.

---

## Fix 1 — BUG #1: off-by-one in `get_current_task` (CRITICAL)

Line 333 currently has:
```python
    return headings[-1][4:].strip()  # strip "## " prefix
```

`"## "` is 3 characters, not 4. `[4:]` drops the first character of every heading. **This was a spec bug** (the original spec §3.1b had `[4:]`) — it propagated faithfully to the code.

Change to:
```python
    return headings[-1][3:].strip()  # strip "## " prefix (3 chars)
```

Supervisor live reproduction (CONFIRMED BROKEN):
```python
from utils.project_awareness import get_current_task
get_current_task('/path/to/projects/crabcakes')  # '026-07-20 — In-flight loops' (WRONG)
```

After fix, expected: `'2026-07-20 — In-flight loops'`

---

## Fix 2 — BUG #2: cache staleness on same-second writes (HIGH)

Line 594 currently has:
```python
        if cached and cached[0] >= mtime:
            return cached[1]
```

The cache key is only `(mtime, dict)`. On filesystems with 1-second mtime granularity, two writes within the same second have identical mtimes, so the second `build_awareness_dict` call returns the stale cached dict — including a stale `CURRENT_TASK`.

Supervisor reproduction (CONFIRMED BROKEN):
```python
save_project_context(d, '## Task1 complete\n')
d1 = build_awareness_dict(d)
save_project_context(d, '## Task1 complete\n\n## Task2 in progress\n')
d2 = build_awareness_dict(d)
d2['CURRENT_TASK']  # 'ask2 in progress' (stale + off-by-one) — should be 'Task2 in progress'
```

**Fix:** add a content fingerprint to the cache key. Use `len(context)` — cheap and sufficient (any real content change alters length, and even same-length edits are caught by the mtime on the next tick).

Find the cache-read block (lines ~591-594):
```python
        mtime = _awareness_dir_mtime(project_path)
        cached = _AWARENESS_CACHE.get(project_path)
        if cached and cached[0] >= mtime:
            return cached[1]
```

Change to compute a content fingerprint and include it in the cache key:
```python
        mtime = _awareness_dir_mtime(project_path)
        cached = _AWARENESS_CACHE.get(project_path)
        # Cache key includes context.md content length so same-second writes
        # with different content invalidate the cache (filesystem mtime is
        # typically 1-second granularity). See FIX audit BUG #2.
        _ctx_for_fp = load_project_context(project_path)
        _content_fp = len(_ctx_for_fp)
        if cached and cached[0] >= mtime and cached[2] == _content_fp:
            return cached[1]
```

And update the cache WRITE block (line 668) to store the fingerprint as the third tuple element:
```python
        _AWARENESS_CACHE[project_path] = (mtime, parts, _content_fp)
```

Update the type annotation at line 60:
```python
_AWARENESS_CACHE: dict[str, tuple[float, dict, int]] = {}
```

**Note:** `_ctx_for_fp` is the same file `load_project_context` reads later in the function. Reading it twice is fine — it's a small file (capped at 50KB) and file I/O from page cache is fast. Do not try to deduplicate the read; keep the change surgical.

---

## Fix 3 — BUG #3: live dict alias returned from cache (MEDIUM)

Line 669 currently has:
```python
    return parts
```

This returns the same dict object stored in `_AWARENESS_CACHE`. A caller mutating the returned dict poisons the cache for all future calls.

Supervisor reproduction (CONFIRMED BROKEN):
```python
d1 = build_awareness_dict(d)
d1['CURRENT_TASK'] = 'TAMPERED'
d2 = build_awareness_dict(d)
d1 is d2  # True
d2['CURRENT_TASK']  # 'TAMPERED' (cache poisoned)
```

**Fix:** return a shallow copy. The values are all strings (immutable), so a shallow copy is sufficient — no need for `copy.deepcopy`.

Change line 669 to:
```python
    return dict(parts)  # shallow copy — values are immutable strings
```

**IMPORTANT:** the cache must still store the ORIGINAL `parts` (line 668 unchanged: `_AWARENESS_CACHE[project_path] = (mtime, parts, _content_fp)`). Only the RETURNED value is copied. Do not change line 668.

---

## Fix 4 — BUG #5: add the 5 missing Phase 1 tests (HIGH)

Add these 5 tests to `tests/test_project_awareness.py`. They come from SPEC §3.4 (the subset applicable to Phase 1 read-path; the append/lifecycle tests belong to a later phase).

Place them after the existing `TestBuildAwarenessBlock` class and before `TestBuildAwarenessSnapshot` (or at the end of the file — your choice, but group them logically).

You will need to add imports at the top of the file. The existing imports are:
```python
from utils.project_awareness import (
    CRABCAKES_DIR_NAME,
    append_project_context,
    build_awareness_block,
    build_awareness_snapshot,
    detect_tech_stack,
    get_crabcakes_dir,
    init_project_config,
    load_project_context,
    load_project_manifest,
    load_team,
    save_awareness_snapshot,
    save_project_context,
    save_team,
)
```

Add `get_current_task` and `build_awareness_dict` to the import list (the latter may already be imported later in the file under `TestAwarenessCaps` — check first; if so, only add `get_current_task`).

The 5 tests:

```python
class TestGetCurrentTask:
    """SPEC-CONTEXT-MD-SYSTEM-FIX §3.4 — get_current_task and CURRENT_TASK injection."""

    def test_get_current_task_returns_last_heading(self, tmp_path):
        """Last '## ' heading text is returned, prefix stripped correctly."""
        init_project_config(str(tmp_path), "p")
        save_project_context(str(tmp_path),
            "## 2026-07-20 — Phase A1 complete\n\n"
            "## 2026-07-21 — Phase A2 in progress\n")
        assert get_current_task(str(tmp_path)) == "2026-07-21 — Phase A2 in progress"

    def test_get_current_task_empty_context(self, tmp_path):
        """Empty context.md returns empty string."""
        init_project_config(str(tmp_path), "p")
        assert get_current_task(str(tmp_path)) == ""

    def test_get_current_task_no_headings(self, tmp_path):
        """Context with no '## ' headings returns empty string."""
        init_project_config(str(tmp_path), "p")
        save_project_context(str(tmp_path), "just plain text\nno headings here\n")
        assert get_current_task(str(tmp_path)) == ""

    def test_current_task_in_awareness_dict(self, tmp_path):
        """build_awareness_dict populates CURRENT_TASK from the last heading."""
        init_project_config(str(tmp_path), "p")
        save_project_context(str(tmp_path), "## 2026-07-20 — Phase A1 complete\n")
        d = build_awareness_dict(str(tmp_path))
        assert d["CURRENT_TASK"] == "2026-07-20 — Phase A1 complete"


class TestContextReadCap:
    """SPEC-CONTEXT-MD-SYSTEM-FIX §3.4 — read cap increased from 3000 to 8000."""

    def test_read_cap_8000_allows_content_beyond_old_3000_limit(self, tmp_path):
        """PROJECT_MEMORY includes content beyond the old 3000-char limit (up to 8000)."""
        init_project_config(str(tmp_path), "p")
        # 5000 chars — would be truncated under the old 3000 cap, fits under 8000
        marker_start = "MARKER_START_"
        content = marker_start + ("x" * 5000)
        save_project_context(str(tmp_path), content)
        d = build_awareness_dict(str(tmp_path))
        # The marker near the start is always present; the point is no truncation
        # message appears because 5000 < 8000
        assert marker_start in d["PROJECT_MEMORY"]
        assert "[... context memory truncated ...]" not in d["PROJECT_MEMORY"]

    def test_read_cap_8000_truncates_above_limit(self, tmp_path):
        """Content > 8000 chars produces a truncation message."""
        init_project_config(str(tmp_path), "p")
        content = ("x" * 10000)
        save_project_context(str(tmp_path), content)
        d = build_awareness_dict(str(tmp_path))
        assert "[... context memory truncated ...]" in d["PROJECT_MEMORY"]
```

---

## Optional: regression tests for the cache bugs (recommended, not required)

If you have time, add these two tests after the above. They guard BUG #2 and BUG #3 from regressing. If you skip them, that's acceptable — the 5 tests above are the spec requirement.

```python
class TestAwarenessCacheFixes:
    """Regression tests for audit BUG #2 (staleness) and BUG #3 (alias)."""

    def test_cache_invalidates_on_rapid_write(self, tmp_path):
        """Two writes within the same mtime tick return different CURRENT_TASK."""
        init_project_config(str(tmp_path), "p")
        save_project_context(str(tmp_path), "## Task1 complete\n")
        d1 = build_awareness_dict(str(tmp_path))
        # Write again immediately (same filesystem second)
        save_project_context(str(tmp_path), "## Task1 complete\n\n## Task2 in progress\n")
        d2 = build_awareness_dict(str(tmp_path))
        assert d2["CURRENT_TASK"] == "Task2 in progress", \
            f"Cache returned stale value: {d2['CURRENT_TASK']!r}"

    def test_returned_dict_isolated_from_cache(self, tmp_path):
        """Mutating the returned dict does not poison the cache."""
        init_project_config(str(tmp_path), "p")
        save_project_context(str(tmp_path), "## Real task\n")
        d1 = build_awareness_dict(str(tmp_path))
        d1["CURRENT_TASK"] = "TAMPERED"
        d2 = build_awareness_dict(str(tmp_path))
        assert d2["CURRENT_TASK"] == "Real task", \
            f"Cache was poisoned by caller mutation: {d2['CURRENT_TASK']!r}"
```

---

## Verification (paste full output)

1. `grep -n "headings\[-1\]" utils/project_awareness.py` — expect `[3:]` not `[4:]`
2. `python3 -c "from utils.project_awareness import get_current_task; print(repr(get_current_task('/path/to/projects/crabcakes')))"` — expect starts with `'2026-` (the leading `2`)
3. `grep -n "cached\[2\] == _content_fp\|tuple\[float, dict, int\]\|_content_fp" utils/project_awareness.py` — expect 3+ matches
4. `grep -n "return dict(parts)" utils/project_awareness.py` — expect 1 match
5. `python3 -m pytest tests/test_project_awareness.py -v 2>&1 | tail -15` — all tests pass (28 existing + 5 new + 2 optional)
6. `python3 -m pytest tests/test_project_awareness.py::TestGetCurrentTask tests/test_project_awareness.py::TestContextReadCap -v` — the 5 new tests pass specifically

---

## COMPLETENESS checklist (mandatory)

```
COMPLETENESS:
- [x/not done] Fix 1: Changed [4:] to [3:] in get_current_task — evidence: <grep + live repro>
- [x/not done] Fix 2: Added content fingerprint to cache key — evidence: <grep>
- [x/not done] Fix 3: Return dict(parts) copy — evidence: <grep>
- [x/not done] Fix 4: Added 5 new tests — evidence: <pytest output>
- [x/not done] All tests pass — evidence: <pytest tail>
```
