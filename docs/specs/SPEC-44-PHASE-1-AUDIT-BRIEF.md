# SPEC-44 Phase 1 — Audit Brief

**Supervisor:** Supervisor
**Auditor:** Debugger
**Type:** Between-phase audit (code-bearing turn) — Phase 1 has LANDED
**Spec:** `docs/specs/SPEC-44-SEARCH-FILES-COUNTS-CONTEXT.md` §2.1, §2.4, §3, §6
**Phase instructions the builder followed:** `docs/specs/SPEC-44-PHASE-1-INSTRUCTIONS.md`
**Files in scope:** `agent/tools.py`, `tests/test_tools.py`

Load `prompts/adversarialDebugger.md` fresh. Work through all 11 sections against the code that landed. One probe per section minimum.

**Do not fix anything. Do not edit any file. Report only.**

---

## What landed

| Edit | Location | What |
|---|---|---|
| 1 | `agent/tools.py:971` | `_match_count_header(rel, size_str, shown, total, name_only=False)` helper |
| 2 | `agent/tools.py:1001` | `count_only: bool = False` param on `_search_files`; docstring updated |
| 3 | `agent/tools.py:1134-1149` | emission loop: `or []` guard, `total`, `shown`, `name_only`, header call, `continue` in count_only, footer+blank line inside `if not count_only:` |
| 4 | `agent/tools.py:1494-1495` | registration lambda forwards `count_only` |
| 5 | `agent/tools.py:1477-1480`, `:1488` | description replaced (146 chars) + `count_only` schema property |
| 6 | `tests/test_tools.py:697-811` | 10 new tests in `TestSearchFiles` |

---

## Supervisor's own verification (already run — do not just repeat it)

```python
# 3-match file
three.py (0KB, 3 matches)
  Line 1: def foo_1(): pass
  Line 2: def foo_2(): pass
  Line 3: def foo_3(): pass

[Open with read_file line=N only to edit.]

# 41-match file
many.py (0KB, 40 of 41 matches)
-> exactly 40 match lines emitted (cap intact)

# count_only=True THROUGH execute_tool (the public path)
many.py (0KB, 41 matches)
-> no footer, no Line lines

# name-only, case-insensitive (pattern TARGET, file zzz_target.py)
zzz_target.py (0KB, name match)
  [name match only — use read_file for content]
-> success=True, no crash

# no matches
'(no matches)'   (exact, preserved)
```

Pytest: **92 passed, 3 failed** — the 3 are `TestToolSchemaSizeBudget` for coder (6299) / debugger (5864) / supervisor (5844), all pre-existing at HEAD (6155/5720/5700 before this work).

`grep -n "min(preview_lines, 40)" agent/tools.py` → exactly 1 hit at `:1021`.

---

## What to attack

### 1. The three traps the spec warned about — are they really avoided?
- `hits = grep_hits.get(rel) or []` at `:1134`. Verify there is no remaining bare `len(hits)` or subscript on a possibly-`None` value anywhere in `_search_files`.
- `rel in name_matches` at `:1137`. Verify `name_matches` is bound on **every** path reaching the emission loop (file-path branch, directory branch, and the early-return `Not a file or directory` branch). Is there any path where it is unbound or stale from a previous call?
- `shown = total if count_only else len(hits[:preview_lines])`. Verify there is no third path and that `shown == total` in `count_only` mode is what the header actually receives.

### 2. The `(no matches)` and existing-contract preservation
- The `ordered` empty early-return still yields the exact string `(no matches)`.
- `test_search_no_match` (exact equality), `test_search_file_path_returns_the_line` (`"Line 3:"`), `test_search_file_type_filter`, `test_search_file_type_filter_survives_name_cap` — all still pass. Confirm by running them.
- `_file_search` (the legacy hidden tool) now emits counts too, because it delegates. Its PARAMETERS were to stay frozen. Verify the `file_search` lambda was not touched.

### 3. The count semantics
- Is `total` really the number of matching **lines**, not occurrences? (grep emits one line per matching line; two matches on one line produce ONE hit.) Verify with a fixture where one line contains the pattern twice.
- Is the name-only labeling correct when a file is **both** a name match and a content match? (`name_only = (not hits) and rel in name_matches` — with content hits, `name_only` is False, so it reports the content count. Correct per spec. Verify.)
- Is `shown < total` the right truncation test? What if `preview_lines` is caller-set larger than 40 — the clamp is `min(preview_lines, 40)`, so can `shown` ever exceed 40?

### 4. The `count_only` mode
- Does it truly emit NO content lines and NO footer, and is the trailing blank line absent too? (`"\n".join` on a list with no trailing `""` — check the exact output shape; does it end with a newline or not?)
- Does `count_only` interact correctly with the `MAX_EXEC_OUTPUT` truncation at the end?
- Does `count_only` on a name-only file emit the `[name match only ...]` line? Per spec it must NOT.

### 5. The registration lambda
- Does `count_only=False` in the lambda signature shadow or conflict with anything in `execute_tool`'s arg injection (`project_path`, `session_key`, `approval_callback`, `scratch_dir` all go into `**kwargs`)?
- Does passing an unknown extra kwarg still get silently swallowed (as before), or did the change alter that?
- Is `file_search`'s lambda genuinely unmodified?

### 6. Tests — are they real?
The 10 new tests must not be tautological. For each, check it would FAIL if the implementation regressed:
- `test_search_count_only_returns_counts` — does it assert the ABSENCE of the footer, or only the presence of a count?
- `test_search_footer_pinned` / `test_search_line_indent_pinned` — do they assert the exact string including the two-space indent, or something looser?
- `test_search_reports_true_total_when_capped` — does it assert `40 of 41` exactly, or just that a number appears?
- `test_search_name_match_does_not_crash` — would it fail if the `or []` guard were removed?
- `test_search_count_reaches_public_tool_path` — would it fail if the lambda edit were reverted? (This is the highest-value test; verify it distinguishes the public path from a direct `_search_files` call.)
- Do any of the 10 duplicate each other?

### 7. Scope creep
Verify the diff touches ONLY `agent/tools.py` and `tests/test_tools.py`. Specifically: no `MAX_CONTEXT_LINES`, no `context` param, no summary line, no `max_results` schema change, no `TOOL_PAYLOAD_BUDGET` change, no edits to `agent/context.py` or `agent/runtime.py`.

---

## Deliverable

Report in **BUG #[N]** format: Severity / Assumption violated / Attack vector / Reproduction / Root cause / Fix.

State explicitly **"no bugs found"** if the probe comes back clean.

If Phase 1 is clean, say so explicitly and state that Phase 2 may start. If it needs fixes, name exactly what and where.

Do not edit files. Do not commit. Report to the Supervisor only.
