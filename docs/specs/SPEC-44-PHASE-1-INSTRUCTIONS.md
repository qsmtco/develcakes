# SPEC-44 Phase 1 of 3 — search_files match counts + count_only

**Builder:** Coder
**Supervisor:** Supervisor
**Spec:** `docs/specs/SPEC-44-SEARCH-FILES-COUNTS-CONTEXT.md` (rev. 4) — read §2.1, §2.4, §3, §6 first
**Rules prompt:** `prompts/steelFramedCodeWriter.md` — apply it
**Files to change:** `agent/tools.py`, `tests/test_tools.py` — **only these two**

Read the spec section §2.1 in full before editing. This file is the phase scope; the spec is the contract.

---

## What Phase 1 is

Each file group header in `search_files` output gains the true match count, plus a new `count_only` mode that returns counts without content lines. Nothing about context lines (that is Phase 2 — do not start it).

**The 40-line preview cap does NOT change.** It is a captain's standing order. `preview_lines = min(preview_lines, 40)` stays exactly as it is.

---

## Edit 1 — `agent/tools.py`: add the header helper

Add this function immediately before `_search_files` (it currently starts at `def _search_files(`):

```python
def _match_count_header(rel: str, size_str: str, shown: int, total: int,
                         name_only: bool = False) -> str:
    """Per-file group header with the true match count.

    ``shown`` is how many match lines the preview cap let through; ``total``
    is the file's true count of matching LINES (not occurrences). When the
    cap truncated, the header says so: "3 of 57 matches". Otherwise:
    "3 matches".

    ``name_only`` is True for a file that matched only by filename. Those
    files have no content matches; they are labeled "name match" rather
    than "0 matches" so a case-insensitive name hit is not misread as a
    file with zero hits.
    """
    if name_only:
        return f"{rel} ({size_str}, name match)"
    if total <= 0:
        return f"{rel} ({size_str}, 0 matches)"
    if shown < total:
        return f"{rel} ({size_str}, {shown} of {total} matches)"
    return f"{rel} ({size_str}, {total} matches)"
```

---

## Edit 2 — `agent/tools.py`: add the `count_only` parameter

In the `_search_files` signature, after `preview_lines: int = 40,`:

```python
    count_only: bool = False,
```

Also update the `_search_files` docstring — the sentence *"The hit file is not opened to count lines."* stays TRUE in Phase 1 (we do not open files in Phase 1), so leave that sentence alone for now. But add to the docstring: the header now carries a match count.

---

## Edit 3 — `agent/tools.py`: the emission loop

This is the current code (inside `_search_files`, near the end):

```python
    project_real = os.path.realpath(project_path)
    lines: list[str] = []
    for rel in ordered:
        full_path = os.path.join(project_real, *rel.split("/"))
        try:
            size = os.stat(full_path).st_size
        except OSError:
            size_str = "?KB"
        else:
            if size < 1024 * 1024:
                size_str = f"{size // 1024}KB"
            else:
                size_str = f"{size // (1024 * 1024)}MB"
        lines.append(f"{rel} ({size_str})")
        hits = grep_hits.get(rel)
        if hits:
            for lineno, content in hits[:preview_lines]:
                lines.append(f"  Line {lineno}: {content.strip()}")
        else:
            lines.append("  [name match only — use read_file for content]")

    lines.append("")
    lines.append("[Open with read_file line=N only to edit.]")
    output = "\n".join(lines)
    if len(output) > MAX_EXEC_OUTPUT:
        output = output[:MAX_EXEC_OUTPUT] + "\n[... truncated ...]"
    return ToolResult(success=True, output=output)
```

Change the per-file body to:

```python
        hits = grep_hits.get(rel) or []
        total = len(hits)
        shown = total if count_only else len(hits[:preview_lines])
        name_only = (not hits) and (rel in name_matches)
        lines.append(_match_count_header(rel, size_str, shown, total, name_only=name_only))
        if count_only:
            continue
        if hits:
            for lineno, content in hits[:preview_lines]:
                lines.append(f"  Line {lineno}: {content.strip()}")
        else:
            lines.append("  [name match only — use read_file for content]")
```

And change the tail — in `count_only` mode the trailing empty line AND the footer are both omitted:

```python
    if not count_only:
        lines.append("")
        lines.append("[Open with read_file line=N only to edit.]")
    output = "\n".join(lines)
```

**Three traps the audit found — do not hit them:**

1. **`hits` is `None`, not `[]`, for a name-only file.** `grep_hits.get(rel)` returns `None`. You MUST use `grep_hits.get(rel) or []`. A bare `len(hits)` raises `TypeError` on the first name-only file of every search.
2. **Use `name_matches`** — the existing local list in `_search_files`. Do NOT invent a `name_match_set`; it does not exist in the source and would `NameError`.
3. **`shown = total` in `count_only` mode.** No preview slice is applied there, so a 41-match file reads `41 matches`, not `40 of 41 matches`.

---

## Edit 4 — `agent/tools.py`: forward `count_only` through the registration lambda

This is critical. The `search_files` lambda currently reads:

```python
        lambda pattern, project_path, path=None, file_type=None, **kwargs:  # type: ignore
            _search_files(pattern, project_path, path, file_type),
```

The `**kwargs` catch-all **silently discards** an unforwarded parameter — no error, no warning, just a wrong answer. Change it to:

```python
        lambda pattern, project_path, path=None, file_type=None, count_only=False, **kwargs:  # type: ignore
            _search_files(pattern, project_path, path, file_type, count_only=count_only),
```

Do NOT touch the `file_search` registration lambda. Its parameters are frozen; it is hidden from the API schema.

---

## Edit 5 — `agent/tools.py`: schema property + description

In the `search_files` `ToolDefinition`, add to `parameters.properties`:

```python
                    "count_only": {"type": "boolean", "description": "Counts only, no lines. Header shows true total."},
```

Replace the description with exactly this (146 characters — measured; do not expand it, the schema is already over budget):

```python
            description=(
                "Find files by name or content. Per file: path, size, match count, "
                "up to 40 matching lines. count_only: counts only, no lines. "
                "Times out after 10s."
            ),
```

Note the `# NOTE: descriptions below stay one line each; do not expand.` comment above `requires_approval` — respect it, keep the density.

---

## Edit 6 — `tests/test_tools.py`: 10 new tests

Add to `TestSearchFiles` (that class starts around line 589; the last test in it is `test_search_file_path_miss_and_type_and_missing`). Add all ten:

| Test | What it asserts |
|---|---|
| `test_search_reports_match_count` | A file with 3 matching lines shows `3 matches` in the header |
| `test_search_reports_true_total_when_capped` | A file with 41 matching lines shows `40 of 41 matches` |
| `test_search_count_only_returns_counts` | `count_only=True` shows the count, NO `Line ` entries, NO footer |
| `test_search_count_only_shows_true_total` | `count_only=True` on a 41-match file shows `41 matches`, NOT `40 of 41` |
| `test_search_count_only_name_match` | A name-only match shows `name match` |
| `test_search_count_reaches_public_tool_path` | `execute_tool("search_files", {"pattern": ..., "count_only": True}, proj)` shows the count — this is the test that catches a missed lambda edit |
| `test_search_count_preserves_line_format` | `"  Line "` (with the two-space indent) still appears for match lines |
| `test_search_footer_pinned` | `"[Open with read_file line=N only to edit.]"` is present — **new pin**, nothing pinned it before |
| `test_search_line_indent_pinned` | `"\n  Line 3: "` is present — **new pin** of the two-space indent |
| `test_search_name_match_does_not_crash` | A search whose FIRST result is a name-only match returns `success=True` — the `or []` guard |

For the 41-match fixture, write a file with 41 lines of `def foo_N(): pass` (N = 0..40) and search for `def foo_`.

**Do not weaken these existing tests.** `test_search_no_match` asserts `r.output == "(no matches)"` — the `(no matches)` early return is unchanged, so it still passes. `test_search_file_path_returns_the_line` asserts `"Line 3:" in r.output` — still true.

---

## Verification you must paste

Run and paste the **full output** of:

```
.venv/bin/python -m py_compile agent/tools.py
.venv/bin/python -m pytest tests/test_tools.py -q
```

**Expected:** 3 failed, and the only failures are `TestToolSchemaSizeBudget` (coder / debugger / supervisor). Those three are **pre-existing at HEAD** — verified by the Supervisor: coder 6155, debugger 5720, supervisor 5700 against a 4000 budget, before any of this work. Do not fix them, do not touch `TOOL_PAYLOAD_BUDGET`. Your change adds ~+130 chars to each; report the new measured numbers if the assertions print them.

Also paste:

```
grep -n "min(preview_lines, 40)" agent/tools.py
grep -n "count_only" agent/tools.py
```

The first must show exactly one hit (the cap is unchanged). The second must show `count_only` in **both** the lambda signature and the `_search_files(...)` call.

---

## COMPLETENESS checklist — mandatory

End your report with exactly this block, each line marked `[x]` or `[not done]` with evidence:

```
COMPLETENESS:
- [x/not done] Edit 1: _match_count_header helper added — <where, line>
- [x/not done] Edit 2: count_only param on _search_files — <line>
- [x/not done] Edit 3: emission loop (or [] guard, shown, name_only, count_only branch, footer omitted) — <lines>
- [x/not done] Edit 4: registration lambda forwards count_only — <line>
- [x/not done] Edit 5: schema property + description — <lines>
- [x/not done] Edit 6: 10 tests added — <line range>
- [x/not done] py_compile clean — paste output
- [x/not done] pytest output pasted — 3 pre-existing TestToolSchemaSizeBudget failures only
- [x/not done] grep min(preview_lines, 40) = 1 hit
- [x/not done] grep count_only shows lambda signature AND call site
```

**Do not silently fix anything outside these six edits.** If you spot a related bug, report it as "related issue found, not fixed in this phase" in the checklist.

**Scope lock:** `agent/tools.py` and `tests/test_tools.py` only. No context lines, no `MAX_CONTEXT_LINES`, no summary line, no `max_results` changes, no schema-budget fixes, no `agent/context.py`, no `agent/runtime.py`.
