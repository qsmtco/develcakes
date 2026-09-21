# Phase 3 Audit Fix Round 2 — 4 bugs

**Spec:** Phase 3 audit findings (Debugger round 2)
**File:** `utils/project_awareness.py` + `tests/test_project_awareness.py`

4 bugs to fix, all confirmed by Supervisor's own reproduction. BUG #4 from the audit (`##\t`) was NOT reproduced by the supervisor and is excluded — do not "fix" it.

**FIRST ACTION: Load the steelFramedCodeWriter prompt fresh** from `/path/to/projects/crabcakes/prompts/steelFramedCodeWriter.md`. Read it in full, activate it, begin with Discovery Phase block.

Read `utils/project_awareness.py` in full before editing.

---

## Edit 1 — BUG #2: `_signals_completion` false positives (HIGH, same substring-overmatch pattern)

**Confirmed by Supervisor:** `_signals_completion("abandoned approach")` returns `True` because `"done" in "abandoned"`. Same bug class as the `_mark_superseded` substring fix.

Find `_signals_completion` (line ~309):

```python
def _signals_completion(entry: str) -> bool:
    """..."""
    entry_lower = entry.lower()
    return any(w in entry_lower for w in ("complete", "completed", "done", "finished", "✅"))
```

Replace with word-boundary regex (compile once at module level for efficiency). Add a module-level constant BEFORE the function:

```python
# Word-boundary completion detection. Prevents false positives like
# "abandoned", "undone", "incomplete", "condone" matching "done".
# See Phase 3 audit BUG #2.
_COMPLETION_RE = re.compile(
    r"\b(?:complete|completed|done|finished)\b|✅",
    re.IGNORECASE,
)


def _signals_completion(entry: str) -> bool:
    """Return True if an entry signals phase/task completion.

    Uses word-boundary matching so "abandoned", "undone", "incomplete",
    "condone" do NOT trigger completion. Detects: complete, completed, done,
    finished (case-insensitive), and the ✅ emoji.

    See SPEC-CONTEXT-MD-SYSTEM-FIX.md §3.1d + Phase 3 audit BUG #2.
    """
    return bool(_COMPLETION_RE.search(entry))
```

**Why `✅` is outside the `\b` group:** emoji are not word characters, so `\b` doesn't work predictably around them. `✅` is matched as a literal substring (it's unambiguous — no English word contains ✅).

---

## Edit 2 — BUG #1: preamble promoted to heading (HIGH, data corruption)

**Confirmed by Supervisor:** `'Some preamble text'` becomes `'## Some preamble text'` after append.

Find `_split_entries` (line ~394). The current preamble-handling block:

```python
    if parts and parts[0].strip():
        # Prepend to the next part (the first real heading body)
        if len(parts) > 1:
            parts[1] = parts[0] + "\n\n## " + parts[1]
        # If there's only the preamble and no headings, return empty
    # Re-add the '## ' prefix to each entry body (the split consumed it)
    entries = ["## " + p.strip() for p in parts[1:] if p.strip()]
    return entries
```

Replace with preamble-as-body (NOT preamble-as-heading):

```python
    if parts and parts[0].strip():
        # Prepend preamble to the first real entry's BODY (not as a heading).
        # The preamble is non-heading content; promoting it to '## ' would
        # corrupt the entry structure. See Phase 3 audit BUG #1.
        if len(parts) > 1:
            parts[1] = parts[0].rstrip() + "\n\n" + parts[1]
        # If there's only the preamble and no headings, return empty
    # Re-add the '## ' prefix to each entry body (the split consumed it)
    entries = ["## " + p.strip() for p in parts[1:] if p.strip()]
    return entries
```

The only change is `parts[0] + "\n\n## " + parts[1]` → `parts[0].rstrip() + "\n\n" + parts[1]`. The preamble text is prepended to the first entry's body, and the existing `## ` re-addition (the list comprehension) adds the heading prefix to the real heading, not the preamble.

---

## Edit 3 — BUG #3: non-standard separators (MEDIUM, format-fragility)

**Confirmed by Supervisor:** en-dash, hyphen, colon separators not recognized.

Find the separator loop in `_extract_phase_id` (line ~343):

```python
    # Split on em-dash (—) or double-hyphen (--) if present
    for sep in ("—", "--"):
        if sep in heading:
```

Expand the separator list (em-dash first, then en-dash, then double-hyphen, then single hyphen, then colon — order matters: longer/more-specific first):

```python
    # Split on common date/phase separators. Em-dash is the canonical format,
    # but en-dash, hyphen, and colon appear in real-world entries.
    # See Phase 3 audit BUG #3.
    for sep in ("—", "–", "--", "-", ":"):
        if sep in heading:
```

**Important ordering note:** `"-"` must come AFTER `"--"` in the list, otherwise `"--"` would never be reached (the single-hyphen check splits first). The list above is correctly ordered.

Also strip leading separators from the result (handles BUG #7 double-em-dash as a bonus). Find the line after the split (currently `after = heading.split(sep, 1)[1].strip()`):

```python
            after = heading.split(sep, 1)[1].strip().lstrip("—–-:").strip()
```

---

## Edit 4 — BUG #5: code blocks split as headings (MEDIUM, markdown-naive-parsing)

**Confirmed by Supervisor:** `## ` inside a triple-backtick code block is split as a new entry.

This requires replacing the regex-based `_split_entries` with a state-machine parser that tracks code-block context. Replace the ENTIRE `_split_entries` function body.

Find `_split_entries` (the function you just edited in Edit 2). Replace its ENTIRE body with:

```python
def _split_entries(content: str) -> list[str]:
    """Split context.md content into individual entries by '## ' heading delimiter.

    Each entry starts with '## ' (the standard dated-heading format). Content
    before the first '## ' heading (if any) is prepended to the first entry's
    body, or dropped if it's only whitespace.

    Code-block awareness: '## ' inside a fenced code block (triple backticks)
    is NOT treated as a heading boundary. See Phase 3 audit BUG #5.

    Returns a list of entry strings, each starting with '## '. Empty list if
    content is empty or has no '## ' headings.
    """
    if not content.strip():
        return []

    lines = content.split("\n")
    preamble: list[str] = []
    entries: list[str] = []
    current: list[str] = []
    in_code_block = False
    seen_heading = False

    for line in lines:
        # Track code-block context (triple backtick fence).
        if line.lstrip().startswith("```"):
            in_code_block = not in_code_block
            if current:
                current.append(line)
            elif preamble:
                preamble.append(line)
            continue

        # A heading boundary is '## ' at line start, OUTSIDE a code block.
        if not in_code_block and line.startswith("## "):
            if not seen_heading:
                # First heading — flush preamble into the new entry's body
                seen_heading = True
                current = list(preamble) + [line] if preamble else [line]
                preamble = []
            else:
                # Subsequent heading — flush current entry, start new one
                if current:
                    entries.append("\n".join(current))
                current = [line]
        elif not seen_heading:
            # Before any heading — accumulate as preamble
            preamble.append(line)
        else:
            # Inside an entry body
            if current:
                current.append(line)

    # Flush the last entry
    if current:
        entries.append("\n".join(current))

    # If there was preamble but no headings at all, return empty
    # (append_project_context's fallback handles non-heading content).
    return [e.strip() for e in entries if e.strip()]
```

**Note:** This is a full rewrite of `_split_entries`. The preamble handling from Edit 2 is now integrated into this state machine (preamble is accumulated separately and prepended to the first entry's body, NOT promoted to a heading). Edit 2's one-line fix is superseded by this rewrite — if you do Edit 4, you do NOT need Edit 2 separately. **Do Edit 2 first (verify the fix), then Edit 4 (the full rewrite that subsumes it).** Actually — to avoid confusion: do Edit 4 (the full rewrite) and SKIP Edit 2, since Edit 4 already handles the preamble correctly.

**CORRECTION:** Skip Edit 2. Do Edit 4 only. Edit 4's state machine handles preamble correctly (prepends to first entry body, not as heading) AND handles code blocks. Edit 2 was a partial fix; Edit 4 is the complete fix for both BUG #1 and BUG #5.

---

## Edit 5 — Add regression tests for all 4 bugs

Add these tests to `TestAppendProjectContextLifecycle`:

```python
    def test_append_preserves_preamble_not_promoted_to_heading(self, tmp_path):
        """Preamble text before the first '## ' is NOT promoted to a heading (BUG #1).

        The preamble should appear in the first entry's body, not as a '## ' line.
        """
        init_project_config(str(tmp_path), "p")
        save_project_context(str(tmp_path), "Some preamble text\n\n## First heading\nbody\n")
        append_project_context(str(tmp_path), "## 2026-07-20 — New entry\nbody")
        result = load_project_context(str(tmp_path))
        # The preamble must NOT become a '## ' heading
        assert not any(line.startswith("## Some preamble") for line in result.split("\n")), \
            f"Preamble was promoted to heading: {result!r}"
        # The preamble text should still be present somewhere
        assert "Some preamble text" in result

    def test_signals_completion_no_false_positives(self, tmp_path):
        """'abandoned', 'incomplete', 'undone' do NOT trigger supersession (BUG #2)."""
        init_project_config(str(tmp_path), "p")
        save_project_context(str(tmp_path), "## 2026-07-19 — Phase B4 in progress\nWorking.")
        # 'abandoned' contains 'done' as a substring but is NOT a completion
        append_project_context(str(tmp_path), "## 2026-07-20 — abandoned approach\nNot done.")
        result = load_project_context(str(tmp_path))
        assert "[SUPERSEDED]" not in result, \
            f"'abandoned' should not trigger supersession: {result!r}"

    def test_append_supersedes_with_en_dash_separator(self, tmp_path):
        """Supersession works with en-dash separator (BUG #3)."""
        init_project_config(str(tmp_path), "p")
        save_project_context(str(tmp_path), "## 2026-07-19 — Phase B4 in progress\nWorking.")
        # Use en-dash (–) instead of em-dash (—)
        append_project_context(str(tmp_path), "## 2026-07-20 – Phase B4 complete\nDone.")
        result = load_project_context(str(tmp_path))
        assert "[SUPERSEDED]" in result, \
            f"En-dash separator should still allow supersession: {result!r}"

    def test_split_entries_ignores_code_block_headings(self, tmp_path):
        """'## ' inside a code block is not treated as a heading (BUG #5)."""
        init_project_config(str(tmp_path), "p")
        # Content with a code block containing '## '
        save_project_context(str(tmp_path),
            "## Real heading\n"
            "```\n"
            "## inside code\n"
            "```\n"
            "body\n")
        append_project_context(str(tmp_path), "## 2026-07-20 — New entry\nbody")
        result = load_project_context(str(tmp_path))
        # The '## inside code' should NOT have been split as a separate entry
        # that got '## ' re-prepended. Check that it still appears inside a code block.
        assert "## inside code" in result  # content preserved
        # The entry count should be 2 (Real heading + New entry), not 3
        from utils.project_awareness import _split_entries
        entries = _split_entries(result)
        assert len(entries) == 2, \
            f"Expected 2 entries (code block not split), got {len(entries)}: {entries!r}"
```

---

## Verification (paste full output)

1. `grep -n "_COMPLETION_RE\|def _signals_completion" utils/project_awareness.py`
2. `grep -n "in_code_block\|def _split_entries" utils/project_awareness.py`
3. `python3 -c "from utils.project_awareness import _signals_completion; print(_signals_completion('abandoned approach'))"` → must print `False`
4. `python3 -c "import ast; ast.parse(open('utils/project_awareness.py').read()); print('parses OK')"`
5. `python3 -m pytest tests/test_project_awareness.py -v 2>&1 | tail -20` — all tests pass (50 total: 46 existing + 4 new)

---

## COMPLETENESS checklist (mandatory)

```
COMPLETENESS:
- [x/not done] Edit 1: _signals_completion word-boundary regex (BUG #2) — evidence: <python output showing False for abandoned>
- [x/not done] Edit 3: Expanded separators in _extract_phase_id (BUG #3) — evidence: <grep>
- [x/not done] Edit 4: _split_entries state machine with code-block awareness (BUG #1 + #5) — evidence: <grep + test>
- [x/not done] Edit 5: 4 regression tests — evidence: <pytest>
- [x/not done] All tests pass — evidence: <pytest tail>
- [x/not done] (Rule 4) All 4 new tests FAIL on broken code — evidence: <paste or note>
```
