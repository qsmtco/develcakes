# Phase 3 Audit Fix — supersession matching bugs

**Severity:** bug (two bugs found by Supervisor's own functional testing)
**Pattern:** `substring-overmatch`, `format-fragility`

## Root Cause

Two bugs in `_mark_superseded` / `_extract_phase_id`:

### BUG #1: Punctuation-sensitive phase-id extraction (format-fragility)

`_extract_phase_id("## 2026-07-17 — activity-drawer re-audit: COMPLETE\nDone.")` returns `'activity-drawer re-audit:'` (with trailing colon). The existing entry heading is `'## 2026-07-17 — activity-drawer re-audit in progress'` (no colon). The substring match `'activity-drawer re-audit:' in '...activity-drawer re-audit in progress'` is `False`, so no supersession happens.

The colon comes from splitting `: COMPLETE` on the em-dash — the text after the em-dash is `activity-drawer re-audit: COMPLETE`, and the trailing-word strip only removes `COMPLETE` (leaving the colon).

### BUG #2: Substring overmatch (substring-overmatch)

`_extract_phase_id("## 2026-07-20 — Phase A1 complete")` returns `'phase a1'`. This is a substring of `'phase a10 in progress'`, so completing Phase A1 incorrectly marks Phase A10 as `[SUPERSEDED]`.

This is dangerous for the actual crabcakes context.md, which has phases A1, A2, B1-B6 — all vulnerable.

## Fix

Replace the substring matching in `_mark_superseded` with **word-boundary regex matching**. Normalize the phase identifier (strip trailing punctuation) before building the regex.

**File:** `utils/project_awareness.py` only.

**FIRST ACTION: Load the steelFramedCodeWriter prompt fresh** from `/path/to/projects/crabcakes/prompts/steelFramedCodeWriter.md`. Read it in full, activate it, begin with Discovery Phase block.

Read `utils/project_awareness.py` in full before editing. Focus on `_mark_superseded` (line ~363) and `_extract_phase_id` (line ~320).

---

## Edit 1 — Normalize phase identifier in `_extract_phase_id`

In `_extract_phase_id`, strip trailing punctuation (colons, periods, dashes) from the extracted phase identifier BEFORE returning it. The current code returns `after.lower()` which may include a trailing colon.

Find the em-dash split branch (around line 340-350):

```python
    for sep in ("—", "--"):
        if sep in heading:
            # Take everything after the separator, strip status words
            after = heading.split(sep, 1)[1].strip()
            # Remove trailing completion/status words for matching
            for word in ("complete", "completed", "done", "finished", "✅"):
                if after.lower().endswith(" " + word):
                    after = after[: -len(word)].rstrip()
                    break
            return after.lower()
```

Change the `return after.lower()` to strip trailing punctuation:

```python
    for sep in ("—", "--"):
        if sep in heading:
            # Take everything after the separator, strip status words
            after = heading.split(sep, 1)[1].strip()
            # Remove trailing completion/status words for matching
            for word in ("complete", "completed", "done", "finished", "✅"):
                if after.lower().endswith(" " + word):
                    after = after[: -len(word)].rstrip()
                    break
            # Strip trailing punctuation (colons, periods, dashes) that would
            # break substring matching against differently-punctuated headings.
            return after.rstrip(":.-—").rstrip().lower()
```

And do the same for the no-separator branch (the final `return heading.lower()`):

```python
    # No separator — use the whole heading body (minus status words)
    for word in ("complete", "completed", "done", "finished", "✅"):
        if heading.lower().endswith(" " + word):
            heading = heading[: -len(word)].rstrip()
            break
    return heading.rstrip(":.-—").rstrip().lower()
```

This fixes BUG #1: `'activity-drawer re-audit:'` becomes `'activity-drawer re-audit'`, which IS a substring of `'activity-drawer re-audit in progress'`.

---

## Edit 2 — Word-boundary matching in `_mark_superseded`

Replace the substring check `if phase_id in line.lower()` with a word-boundary regex match. This fixes BUG #2: `'phase a1'` will no longer match `'phase a10'`.

Find `_mark_superseded` (line ~363). Currently:

```python
def _mark_superseded(existing: str, new_entry: str) -> str:
    """..."""
    phase_id = _extract_phase_id(new_entry)
    if not phase_id:
        return existing

    lines = existing.split("\n")
    result = []
    for line in lines:
        if line.startswith("## ") and phase_id in line.lower():
            lower = line.lower()
            if any(w in lower for w in ("in progress", "pending", "current task")):
                if "[SUPERSEDED]" not in line:
                    line = line + " [SUPERSEDED]"
        result.append(line)
    return "\n".join(result)
```

Change to use a word-boundary regex instead of substring. Build the regex once outside the loop. **Escape the phase_id with `re.escape`** (it may contain regex metacharacters like `.` or `-`), and wrap it in `\b` word boundaries:

```python
def _mark_superseded(existing: str, new_entry: str) -> str:
    """..."""
    phase_id = _extract_phase_id(new_entry)
    if not phase_id:
        return existing

    # Build a word-boundary regex so 'phase a1' does NOT match 'phase a10'.
    # re.escape handles any regex metacharacters in the phase identifier.
    # The trailing \b prevents suffix matches (a1 matching a10); the leading
    # \b prevents prefix matches.
    try:
        phase_pattern = re.compile(r"\b" + re.escape(phase_id) + r"\b", re.IGNORECASE)
    except re.error:
        # If the phase_id is somehow un-escapable, fall back to no supersession
        # (conservative — don't risk false positives).
        return existing

    lines = existing.split("\n")
    result = []
    for line in lines:
        if line.startswith("## ") and phase_pattern.search(line):
            lower = line.lower()
            if any(w in lower for w in ("in progress", "pending", "current task")):
                if "[SUPERSEDED]" not in line:
                    line = line + " [SUPERSEDED]"
        result.append(line)
    return "\n".join(result)
```

**Why this works:**
- `\bphase a1\b` does NOT match `phase a10` because the `\b` after `a1` requires a word boundary, and `a10` has `0` after `a1` (no boundary).
- `\bactivity-drawer re-audit\b` DOES match `activity-drawer re-audit in progress` because there's a word boundary after `audit` (the space before `in`).
- `re.escape` ensures `-` and other metacharacters are treated literally.

---

## Edit 3 — Add regression tests for both bugs

Add these tests to `TestAppendProjectContextLifecycle` in `tests/test_project_awareness.py`:

```python
    def test_append_supersedes_punctuated_phase(self, tmp_path):
        """Supersession works when new entry has colon-punctuation (BUG: format-fragility).

        're-audit: COMPLETE' must supersede 're-audit in progress' even though
        the colon makes the phase identifiers textually different.
        """
        init_project_config(str(tmp_path), "p")
        save_project_context(str(tmp_path), "## 2026-07-17 — activity-drawer re-audit in progress\nWorking.")
        append_project_context(str(tmp_path), "## 2026-07-17 — activity-drawer re-audit: COMPLETE\nDone.")
        result = load_project_context(str(tmp_path))
        assert "[SUPERSEDED]" in result, f"Colon-punctuated completion should supersede: {result!r}"

    def test_append_does_not_overmatch_phase_suffix(self, tmp_path):
        """Completing Phase A1 must NOT supersede Phase A10 (BUG: substring-overmatch).

        'phase a1' must not match 'phase a10' — word boundary required.
        """
        init_project_config(str(tmp_path), "p")
        save_project_context(str(tmp_path), "## 2026-07-19 — Phase A10 in progress\nWorking A10.")
        append_project_context(str(tmp_path), "## 2026-07-20 — Phase A1 complete\nDone A1.")
        result = load_project_context(str(tmp_path))
        assert "[SUPERSEDED]" not in result, \
            f"Phase A1 must not supersede Phase A10 (substring overmatch): {result!r}"
```

**Rule 4 check (steelFramedCodeWriter):** confirm BOTH new tests FAIL on the current (broken) code before reporting them as passing on the fixed code. Temporarily revert Edits 1+2, run the tests, confirm they fail, then re-apply.

---

## Verification (paste full output)

1. `python3 -c "
from utils.project_awareness import _extract_phase_id
# BUG #1 fix: trailing colon stripped
pid = _extract_phase_id('## 2026-07-17 — activity-drawer re-audit: COMPLETE\nDone.')
print('phase_id:', repr(pid))
assert not pid.endswith(':'), f'Trailing colon not stripped: {pid!r}'
# BUG #2 fix: word boundary
print('phase a1 id:', repr(_extract_phase_id('## 2026-07-20 — Phase A1 complete\nDone.')))
"`
2. `python3 -m pytest tests/test_project_awareness.py::TestAppendProjectContextLifecycle -v 2>&1 | tail -15` — all 9 tests pass (7 existing + 2 new)
3. `python3 -m pytest tests/test_project_awareness.py -q 2>&1 | tail -5` — all 46 tests pass
4. `python3 -c "import ast; ast.parse(open('utils/project_awareness.py').read()); print('parses OK')"`

---

## COMPLETENESS checklist (mandatory)

```
COMPLETENESS:
- [x/not done] Edit 1: Strip trailing punctuation in _extract_phase_id — evidence: <python output showing no trailing colon>
- [x/not done] Edit 2: Word-boundary regex in _mark_superseded — evidence: <grep showing phase_pattern>
- [x/not done] Edit 3: Added 2 regression tests — evidence: <pytest>
- [x/not done] (Rule 4) Both new tests FAIL on broken code — evidence: <paste>
- [x/not done] All tests pass — evidence: <pytest tail>
```
