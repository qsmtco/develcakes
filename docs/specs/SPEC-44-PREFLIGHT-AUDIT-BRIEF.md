# SPEC-44 Pre-Flight Audit Brief

**Date:** 2026-10-20
**Supervisor:** Supervisor
**Auditor:** Debugger
**Type:** Pre-flight (spec-vs-code drift check) — no implementation written yet
**Spec under audit:** `docs/specs/SPEC-44-SEARCH-FILES-COUNTS-CONTEXT.md` (read in full)

Load `prompts/adversarialDebugger.md` fresh. Work through all 11 sections against the code in scope. One probe per section minimum.

**Do not fix anything. Do not edit any file. Report only.** The output of this audit decides whether the spec changes or Phase 1 begins.

---

## Code scope to probe

Read these yourself. Do not trust the spec's line numbers — verify them.

- `agent/tools.py`
  - `_search_files` (~962)
  - `_run_grep` (~890)
  - the grep hit parser (`split(":", 2)`)
  - the output emission loop (~1090-1119)
  - `_file_search` (~1117)
  - the `search_files` and `file_search` tool registrations (~1430-1479)
  - the constants block (~270-285)
- `agent/context.py` — `_find_matching_files` (956)
- `tests/test_tools.py` — `TestSearchFiles` (~589-660)

---

## Specific claims to attack

This is a spec-vs-code drift check. Verify each claim against source; do not reason from the spec's prose.

### C1 — The true total survives the preview cap
The spec claims the count is computed from the full `hits` list *before* the `hits[:preview_lines]` slice, so the header can report `40 of 41 matches`.
Verify: where exactly is the cap applied, and is the full `hits` list still available at header-emission time?

### C2 — The registration lambda swallows new parameters
The spec says the `search_files` lambda forwards only `(pattern, project_path, path, file_type)` and swallows `**kwargs`, and that `count_only` must therefore be threaded through explicitly.
Verify: what happens **today** if a caller passes `count_only` through `execute_tool("search_files", ...)` — silently dropped, or `TypeError`? Trace `execute_tool`'s handler call and its `TypeError` handler. The spec's acceptance criterion ("count_only works through execute_tool") depends on the real behavior.

### C3 — The parser constraint is real, and the plan respects it
The spec claims the hit parser requires `path:lineno:content` via `split(":", 2)`, that grep's `-C` output would not parse, and that context must therefore be produced in Python.
Verify the parser. Verify that the spec's Python-post-processing plan introduces no grep flag change.

### C4 — Name matches vs content matches: is the count ambiguous?
The spec says a name-only match shows `0 matches` and keeps `[name match only — use read_file for content]`, and that a file which is BOTH a name match and has content hits reports the *content* count.
Verify: how do `name_matches` and `grep_hits` merge in the source? Can a file appear in both? Is the proposed count well-defined in that case? Is the spec's explanation sufficient, or does it need to say the name match is visible in the path?

### C5 — The pinned output formats
The spec claims existing tests pin: the `  Line {n}: ` match prefix, the exact `(no matches)` output, and the `[Open with read_file line=N only to edit.]` footer.
Verify each against the actual assertions in `tests/test_tools.py`.

### C6 — The `execute_tool` gate ordering
The spec claims the `allowed_tools` gate runs before the handler, and that an unforwarded parameter surfaces as a `TypeError`-caught tool error rather than a crash.
Verify in `execute_tool`.

### C7 — Internal consistency of the spec's own edge-case table
The spec's edge-case table has a row for `count_only` with more than 40 matches. Read the row and the header-helper logic it describes.
Question: is that row self-consistent with the helper? Does the spec state clearly what `count_only` prints when the file has more than 40 matches — the true total alone, or `N of N matches`? Flag any contradiction between §2.1, §6, and §7.

### C8 — The truncation order
The spec claims `MAX_EXEC_OUTPUT` truncation is the last step in `_search_files`, and that the new summary line and the footer would be cut by it.
Verify the ordering in the source.

---

## Additional free-form probing

Use the 11 sections to find anything the eight claims above do not cover. Specifically consider:
- Does the spec's `count_only` mode interact correctly with the `(no matches)` early return?
- Does the spec's context-window merging rule have a hole (e.g. windows that touch but do not overlap)?
- Does the spec correctly describe the behavior of `max_results` (files) vs the per-file match count?
- Is the spec's claim that `file_search` does NOT gain `count_only` consistent with `_API_SCHEMA_HIDDEN` and the transcript-replay rationale?
- Does the spec introduce any new failure mode for `execute_tool` callers that previously worked?

---

## Deliverable

Report in **BUG #[N]** format: Severity / Assumption violated / Attack vector / Reproduction / Root cause / Fix.

State explicitly **"no bugs found"** if the probe comes back clean. If the spec is wrong, name the section and state what the code actually does.

Do not edit files. Do not commit. Report to the Supervisor only.
