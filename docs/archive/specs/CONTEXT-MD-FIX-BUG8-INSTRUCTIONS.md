# Phase 1 Re-Audit Fix — BUG #8 (cache fingerprint collision)

**FIRST ACTION: Load the steelFramedCodeWriter prompt fresh.** Read `/path/to/projects/crabcakes/prompts/steelFramedCodeWriter.md` IN FULL before writing any code. Activate it. Begin your response with "Starting Discovery Phase — reading all relevant files before writing any code." and output a DISCOVERY block per its Step 0. Follow all 8 core rules: read-before-write, hard-part-first, verify-every-claim, tests-must-be-able-to-fail, wire-it-or-delete-it, validate-all-input, error-handling-not-optional, do-not-modify-what-you-werent-asked-to.

---

## Context

Debugger's re-audit of Phase 1 audit-fixes found BUG #8: the cache fingerprint uses `len(content)`, which collides on same-length different-content writes within the same filesystem mtime tick. The supervisor confirmed this is logically correct — two writes of identical length but different text produce the same `len()` fingerprint, so the cache returns stale `CURRENT_TASK`.

Replace `len()` with a real content hash.

## Files (2)

- `utils/project_awareness.py`
- `tests/test_project_awareness.py`

Read BOTH in full before editing.

---

## Edit 1 — `utils/project_awareness.py`: use sha1 for fingerprint

### 1a. Add `import hashlib`

Find the import block at the top of the file (around lines 13-17):
```python
import json
import logging
import os
import re
import time
```

Add `import hashlib` in alphabetical order. First run `grep -n "^import hashlib" utils/project_awareness.py` to confirm it is NOT already present. If absent, insert it after `import json` (alphabetical: hashlib < json < logging < os < re < time — actually hashlib comes BEFORE json, so insert at the top of the import block):

```python
import hashlib
import json
import logging
import os
import re
import time
```

Wait — verify the actual current order with `sed -n '13,20p'` and place `hashlib` in the correct alphabetical position. Do not reorder existing imports; add only the one new line.

### 1b. Change the fingerprint computation

Find this line (around line 598):
```python
        _content_fp = len(_ctx_for_fp)
```

Change to:
```python
        _content_fp = hashlib.sha1(_ctx_for_fp.encode("utf-8", errors="replace")).hexdigest()
```

The variable is now a `str` (40-char hex digest), not an `int`.

### 1c. Update the type annotation

Find this line (line 60):
```python
_AWARENESS_CACHE: dict[str, tuple[float, dict, int]] = {}
```

Change `int` to `str`:
```python
_AWARENESS_CACHE: dict[str, tuple[float, dict, str]] = {}
```

The comparison `cached[2] == _content_fp` (line ~599) already works correctly for two strings — no change needed there.

---

## Edit 2 — `tests/test_project_awareness.py`: add regression test + strengthen weak test

### 2a. Add BUG #8 regression test

Add this test to the `TestAwarenessCacheFixes` class:
```python
    def test_cache_invalidates_on_same_length_write(self, tmp_path):
        """Same-length different-content writes invalidate the cache (BUG #8).

        len()-based fingerprints collide here; sha1 does not.
        """
        import os
        init_project_config(str(tmp_path), "p")
        content_a = "## Task A complete now\n" + ("A" * 180)
        content_b = "## Task B complete now\n" + ("B" * 180)  # same length, different content
        assert len(content_a) == len(content_b)
        save_project_context(str(tmp_path), content_a)
        d1 = build_awareness_dict(str(tmp_path))
        save_project_context(str(tmp_path), content_b)
        # Pin mtime to force the same-tick scenario
        ctx_path = os.path.join(str(tmp_path), ".crabcakes", "context.md")
        m = os.stat(ctx_path).st_mtime
        os.utime(ctx_path, (m, m))
        d2 = build_awareness_dict(str(tmp_path))
        assert d2["CURRENT_TASK"] == "Task B complete now", \
            f"Cache stale (BUG #8): {d2['CURRENT_TASK']!r}"
```

This test MUST fail on the `len()`-based code and pass on the `sha1`-based code. After implementing, temporarily revert Edit 1b to `len()` and confirm this test fails — that is steelFramedCodeWriter Rule 4 in action.

### 2b. Strengthen the weak truncation test

Find the existing `test_read_cap_8000_truncates_above_limit` in the `TestContextReadCap` class. It currently uses 10000 chars, which would pass under any cap < 10000. Replace its body to test both sides of the 8000 boundary:

```python
    def test_read_cap_8000_truncates_above_limit(self, tmp_path):
        """Truncation boundary is exactly 8000 chars (BUG #9 from re-audit)."""
        init_project_config(str(tmp_path), "p")
        # Exactly at cap — no truncation
        save_project_context(str(tmp_path), "x" * 8000)
        d = build_awareness_dict(str(tmp_path))
        assert "[... context memory truncated ...]" not in d["PROJECT_MEMORY"]
        # One over cap — truncation message present
        save_project_context(str(tmp_path), "x" * 8001)
        d = build_awareness_dict(str(tmp_path))
        assert "[... context memory truncated ...]" in d["PROJECT_MEMORY"]
```

---

## Verification (paste full output)

1. `grep -n "^import hashlib" utils/project_awareness.py` — expect 1 match
2. `grep -n "hashlib.sha1\|_content_fp" utils/project_awareness.py` — expect ≥ 3 matches
3. `grep -n "tuple\[float, dict, str\]" utils/project_awareness.py` — expect 1 match
4. `python3 -c "import ast; ast.parse(open('utils/project_awareness.py').read()); print('parses OK')"`
5. `python3 -m pytest tests/test_project_awareness.py -v 2>&1 | tail -20` — all tests pass
6. `python3 -m pytest tests/test_project_awareness.py::TestAwarenessCacheFixes::test_cache_invalidates_on_same_length_write -v` — the new BUG #8 test passes specifically

---

## COMPLETENESS checklist (mandatory)

```
COMPLETENESS:
- [x/not done] Edit 1a: Added import hashlib — evidence: <grep>
- [x/not done] Edit 1b: Changed len() to hashlib.sha1().hexdigest() — evidence: <grep>
- [x/not done] Edit 1c: Updated type annotation int→str — evidence: <grep>
- [x/not done] Edit 2a: Added test_cache_invalidates_on_same_length_write — evidence: <pytest>
- [x/not done] Edit 2b: Strengthened test_read_cap_8000_truncates_above_limit — evidence: <pytest>
- [x/not done] All tests pass — evidence: <pytest tail>
- [x/not done] (Rule 4) BUG #8 test confirmed to FAIL on len()-based code — evidence: <paste>
```
