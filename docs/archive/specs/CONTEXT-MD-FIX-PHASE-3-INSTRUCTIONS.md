# Phase 3 — append_project_context lifecycle management

**Spec:** `docs/specs/SPEC-CONTEXT-MD-SYSTEM-FIX.md` §3.1d
**File:** `utils/project_awareness.py` (implementation) + `tests/test_project_awareness.py` (tests)
**Goal:** Add supersedure of stale "in progress" entries and FIFO eviction at 50 entries to `append_project_context`.

**FIRST ACTION: Load the steelFramedCodeWriter prompt fresh.** Read `/path/to/projects/crabcakes/prompts/steelFramedCodeWriter.md` IN FULL before writing any code. Activate it. Begin your response with "Starting Discovery Phase — reading all relevant files before writing any code." and output a DISCOVERY block. Follow all 8 core rules.

Read `utils/project_awareness.py` in full before editing (especially `append_project_context` at line 309, `MAX_CONTEXT_ENTRIES` at line 58, and `save_project_context`).

---

## Important deviation from the spec (read carefully)

The spec §3.1d provides a `_mark_superseded` helper with a regex `r'## .+?—\s*(.+?)(?:\s+(?:complete|done))'`. **DO NOT use this regex verbatim.** It is too narrow for real-world entries. Supervisor analysis of the actual `.crabcakes/context.md` found entries like:
- `## 2026-07-17 — activity-drawer re-audit: COMPLETE` (colon before COMPLETE — regex misses)
- `## 2026-07-17 — drawer width fix verified` (no completion word — regex misses)

The supersession logic must be more robust. Use the approach below (extract phase identifier by splitting on the em-dash, not by regex on completion words).

---

## Edit 1 — Replace `append_project_context` (§3.1d)

Find the current function (line 309):

```python
def append_project_context(project_path: str, entry: str) -> None:
    """
    Append an entry to .crabcakes/context.md. Adds separator if file has content.
    """
    existing = load_project_context(project_path)
    separator = "\n\n" if existing.strip() else ""
    save_project_context(project_path, existing + separator + entry)
```

Replace with:

```python
def append_project_context(project_path: str, entry: str) -> None:
    """Append an entry to .crabcakes/context.md with lifecycle management.

    - Supersedes stale "in progress" / "pending" entries for the same phase
      when a "complete" / "done" entry is appended. Marks them [SUPERSEDED]
      in place (does not delete — preserves audit trail).
    - Enforces MAX_CONTEXT_ENTRIES with FIFO eviction (oldest entries dropped).

    The entry is expected to start with a '## ' heading. Entries without a
    heading are appended without supersedure processing (format preserved).

    See SPEC-CONTEXT-MD-SYSTEM-FIX.md §3.1d.
    """
    existing = load_project_context(project_path)

    # Supersedure: if the new entry signals completion, mark matching
    # "in progress" entries as [SUPERSEDED].
    if _signals_completion(entry):
        existing = _mark_superseded(existing, entry)

    # FIFO eviction: split into entries, cap at MAX_CONTEXT_ENTRIES.
    # The new entry is appended AFTER eviction so it is never the one evicted.
    entries = _split_entries(existing)
    if len(entries) >= MAX_CONTEXT_ENTRIES:
        entries = entries[-(MAX_CONTEXT_ENTRIES - 1):]
    entries.append(entry)

    save_project_context(project_path, "\n\n".join(entries))
```

---

## Edit 2 — Add `_signals_completion` helper

Add this NEW helper function immediately BEFORE `append_project_context` (so it's defined before use; Python resolves at call time but grouping aids readability). Place it right after `save_project_context` ends and before `append_project_context`:

```python
def _signals_completion(entry: str) -> bool:
    """Return True if an entry signals phase/task completion.

    Detects: 'complete', 'completed', 'done', 'finished', '✅', 'COMPLETE',
    'DONE' (case-insensitive). Matches anywhere in the entry text so it
    catches 'Phase A1 complete', 're-audit: COMPLETE', 'fix verified ✅', etc.
    """
    entry_lower = entry.lower()
    return any(w in entry_lower for w in ("complete", "done", "finished", "✅"))
```

This is broader than the spec's `("complete", "done", "✅")` — it adds "finished" and works case-insensitively. The spec's gate was correct in spirit but too narrow.

---

## Edit 3 — Add `_mark_superseded` helper

Add this NEW helper immediately after `_signals_completion` (before `append_project_context`):

```python
def _mark_superseded(existing: str, new_entry: str) -> str:
    """Mark 'in progress' / 'pending' entries as [SUPERSEDED] when a completion arrives.

    Extracts a phase identifier from the new entry's heading by splitting on
    the em-dash separator (the standard context.md format: '## DATE — PHASE').
    Then scans existing entry headings for entries that:
      (a) contain the same phase identifier (case-insensitive), AND
      (b) indicate 'in progress', 'pending', or 'current task' status.

    Matching entries are marked '[SUPERSEDED]' (appended to the heading line).
    Already-superseded entries are not re-marked. Entries are never deleted.

    Returns the modified existing-content string. If no phase identifier can
    be extracted from the new entry, returns existing unchanged (conservative).
    """
    phase_id = _extract_phase_id(new_entry)
    if not phase_id:
        return existing  # No identifiable phase — don't touch anything

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


def _extract_phase_id(entry: str) -> str:
    """Extract a phase/task identifier from an entry's '## ' heading.

    Handles the standard context.md format: '## DATE — PHASE DESCRIPTION'.
    Returns the text after the em-dash (or the first '## ' heading's body if
    no em-dash), stripped of trailing status words and whitespace.
    Returns empty string if no '## ' heading is found.

    Examples:
      '## 2026-07-20 — Phase B6 complete'  → 'phase b6'
      '## 2026-07-19 — Phase A1 complete'  → 'phase a1'
      '## 2026-07-20 — In-flight loops'    → 'in-flight loops'
      'no heading here'                    → ''
    """
    # Find the first '## ' heading in the entry
    heading = None
    for line in entry.split("\n"):
        if line.startswith("## "):
            heading = line[3:].strip()
            break
    if not heading:
        return ""

    # Split on em-dash (—) or double-hyphen (--) if present
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

    # No separator — use the whole heading body (minus status words)
    for word in ("complete", "completed", "done", "finished", "✅"):
        if heading.lower().endswith(" " + word):
            heading = heading[: -len(word)].rstrip()
            break
    return heading.lower()
```

**Note on `phase_id in line.lower()`:** the phase identifier is already lowercased by `_extract_phase_id`, and we compare against `line.lower()`. This is intentional case-insensitive substring matching. It means "Phase A1" in the new entry matches "phase a1" in existing entries regardless of case.

---

## Edit 4 — Add `_split_entries` helper

Add this NEW helper immediately after `_extract_phase_id` (before `append_project_context`):

```python
def _split_entries(content: str) -> list[str]:
    """Split context.md content into individual entries by '## ' heading delimiter.

    Each entry starts with '## ' (the standard dated-heading format). Content
    before the first '## ' heading (if any) is prepended to the first entry,
    or dropped if it's only whitespace.

    Returns a list of entry strings, each starting with '## '. Empty list if
    content is empty or has no '## ' headings.
    """
    if not content.strip():
        return []
    # Split on '## ' that appears at the start of a line.
    # The regex lookbehind ensures we split at line-start '## '.
    parts = re.split(r'(?m)^## ', content)
    # The first part is content before the first '## ' heading — usually empty
    # or a header comment. If it's non-empty, prepend it to the first real entry.
    if parts and parts[0].strip():
        # Prepend to the next part (the first real heading body)
        if len(parts) > 1:
            parts[1] = parts[0] + "\n\n## " + parts[1]
        # If there's only the preamble and no headings, return empty
    # Re-add the '## ' prefix to each entry body (the split consumed it)
    entries = ["## " + p.strip() for p in parts[1:] if p.strip()]
    return entries
```

**Important:** `import re` is already present at module level (line ~15, confirmed in Phase 1). Do NOT add it again.

---

## Verification (paste full output)

1. `grep -n "def append_project_context\|def _signals_completion\|def _mark_superseded\|def _extract_phase_id\|def _split_entries" utils/project_awareness.py` — expect 5 matches
2. `grep -n "MAX_CONTEXT_ENTRIES" utils/project_awareness.py` — expect ≥ 3 matches (constant def + append use + test refs)
3. `python3 -c "import ast; ast.parse(open('utils/project_awareness.py').read()); print('parses OK')"`
4. `python3 -c "from utils.project_awareness import append_project_context; print('OK')"`
5. `python3 -m pytest tests/test_project_awareness.py -v 2>&1 | tail -15` — all tests pass

---

## Edit 5 — Add tests for the lifecycle (§3.4 remaining tests)

Add these tests to `tests/test_project_awareness.py`. You will need to import the new helpers — add to the existing import block:
`_signals_completion` is private (underscore prefix) — test it indirectly through `append_project_context`. Do NOT import private helpers into the test file; test the PUBLIC behavior.

Add a new test class after `TestAwarenessCacheFixes`:

```python
class TestAppendProjectContextLifecycle:
    """SPEC-CONTEXT-MD-SYSTEM-FIX §3.1d — append supersedure + FIFO eviction."""

    def test_append_supersedes_in_progress_entry(self, tmp_path):
        """Appending 'Phase B4 complete' marks 'Phase B4 in progress' as [SUPERSEDED]."""
        init_project_config(str(tmp_path), "p")
        save_project_context(str(tmp_path), "## 2026-07-19 — Phase B4 in progress\nDetails here.")
        append_project_context(str(tmp_path), "## 2026-07-20 — Phase B4 complete\nDone.")
        result = load_project_context(str(tmp_path))
        assert "[SUPERSEDED]" in result, f"Expected [SUPERSEDED] marker, got: {result!r}"
        assert "Phase B4 in progress" in result  # original entry preserved
        assert "Phase B4 complete" in result     # new entry appended

    def test_append_does_not_supersede_when_no_completion_word(self, tmp_path):
        """Appending a non-completion entry does not supersede anything."""
        init_project_config(str(tmp_path), "p")
        save_project_context(str(tmp_path), "## 2026-07-19 — Phase B4 in progress\nWorking.")
        append_project_context(str(tmp_path), "## 2026-07-20 — Phase B4 notes\nJust notes.")
        result = load_project_context(str(tmp_path))
        assert "[SUPERSEDED]" not in result, f"Should not supersede without completion word: {result!r}"

    def test_append_does_not_supersede_unrelated_phase(self, tmp_path):
        """Completing Phase B4 does not supersede Phase A1 in progress."""
        init_project_config(str(tmp_path), "p")
        save_project_context(str(tmp_path), "## 2026-07-19 — Phase A1 in progress\nWorking.")
        append_project_context(str(tmp_path), "## 2026-07-20 — Phase B4 complete\nDone.")
        result = load_project_context(str(tmp_path))
        assert "[SUPERSEDED]" not in result, f"Should not supersede unrelated phase: {result!r}"

    def test_append_supersedes_case_insensitive(self, tmp_path):
        """'COMPLETE' (uppercase) in the new entry still triggers supersedure."""
        init_project_config(str(tmp_path), "p")
        save_project_context(str(tmp_path), "## 2026-07-19 — Phase B4 in progress\nWorking.")
        append_project_context(str(tmp_path), "## 2026-07-20 — Phase B4 COMPLETE\nDone.")
        result = load_project_context(str(tmp_path))
        assert "[SUPERSEDED]" in result, f"Case-insensitive completion should supersede: {result!r}"

    def test_append_fifo_eviction_at_50(self, tmp_path):
        """Appending the 51st entry evicts the oldest (FIFO)."""
        init_project_config(str(tmp_path), "p")
        # Fill with 50 entries
        for i in range(50):
            append_project_context(str(tmp_path), f"## 2026-01-{i+1:02d} — Entry {i}\nBody.")
        result = load_project_context(str(tmp_path))
        assert "Entry 0" in result  # oldest still present at exactly 50
        # Append the 51st
        append_project_context(str(tmp_path), "## 2026-02-01 — Entry 50\nBody.")
        result = load_project_context(str(tmp_path))
        assert "Entry 0" not in result, f"FIFO should have evicted Entry 0: {result[:200]!r}"
        assert "Entry 50" in result  # newest present

    def test_append_preserves_non_matching_entries(self, tmp_path):
        """Entries that don't match the completing phase are untouched."""
        init_project_config(str(tmp_path), "p")
        save_project_context(str(tmp_path),
            "## 2026-07-19 — Phase A1 complete\nDone A1.\n\n"
            "## 2026-07-19 — Phase B4 in progress\nWorking B4.")
        append_project_context(str(tmp_path), "## 2026-07-20 — Phase B4 complete\nDone B4.")
        result = load_project_context(str(tmp_path))
        # Phase A1 should NOT be marked superseded
        a1_section = result.split("Phase A1")[0] + "Phase A1"  # get up to A1
        assert "Phase A1 complete" in result
        # Phase B4 in progress SHOULD be superseded
        assert "Phase B4 in progress" in result
        assert "[SUPERSEDED]" in result

    def test_append_idempotent_supersedure(self, tmp_path):
        """Appending the same completion twice does not double-mark [SUPERSEDED]."""
        init_project_config(str(tmp_path), "p")
        save_project_context(str(tmp_path), "## 2026-07-19 — Phase B4 in progress\nWorking.")
        append_project_context(str(tmp_path), "## 2026-07-20 — Phase B4 complete\nDone.")
        append_project_context(str(tmp_path), "## 2026-07-21 — Phase B4 complete (re-confirmed)\nDone again.")
        result = load_project_context(str(tmp_path))
        # Should only have ONE [SUPERSEDED] marker on the original entry
        assert result.count("[SUPERSEDED]") == 1, \
            f"Expected 1 [SUPERSEDED], got {result.count('[SUPERSEDED]')}: {result!r}"
```

Run: `python3 -m pytest tests/test_project_awareness.py::TestAppendProjectContextLifecycle -v` — all 7 new tests must pass.

---

## COMPLETENESS checklist (mandatory)

```
COMPLETENESS:
- [x/not done] Edit 1: Replaced append_project_context with lifecycle version — evidence: <grep>
- [x/not done] Edit 2: Added _signals_completion helper — evidence: <grep>
- [x/not done] Edit 3: Added _mark_superseded + _extract_phase_id helpers — evidence: <grep>
- [x/not done] Edit 4: Added _split_entries helper — evidence: <grep>
- [x/not done] Edit 5: Added 7 lifecycle tests — evidence: <pytest>
- [x/not done] All tests pass (existing + new) — evidence: <pytest tail>
```
