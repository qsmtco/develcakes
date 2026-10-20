# SPEC-44 Re-Audit Brief (rev. 2)

**Date:** 2026-10-20
**Supervisor:** Supervisor
**Auditor:** Debugger
**Type:** Re-audit after spec revision — the pre-flight verdict was "DO NOT IMPLEMENT AS WRITTEN"
**Spec under audit:** `docs/specs/SPEC-44-SEARCH-FILES-COUNTS-CONTEXT.md` — now **rev. 2**, 517 lines (was 396)

Load `prompts/adversarialDebugger.md` fresh. Work through all 11 sections again against the revised spec and the same code scope. One probe per section minimum.

**Do not fix anything. Do not edit any file. Report only.**

---

## What changed in rev. 2

All 20 BUGs from the pre-flight audit are addressed. Every correction is marked inline in the spec with its BUG number so you can audit the fix in place. The substantive changes:

| Was | Now (rev. 2) | BUG |
|---|---|---|
| `hits` is "empty" for a name-only file | `hits` is `None`; the spec mandates `hits = grep_hits.get(rel) or []` and a `name_only` flag | #1 |
| "an unforwarded param surfaces as a TypeError" | It is **silently discarded** by `**kwargs`; the public-path test is load-bearing | #2 |
| `context` never added to the lambda | §2.2 and §5 step 7 both mandate the lambda change, explicitly listed as its own step | #3 |
| Name-only file shows `0 matches` | Shows `name match` (name match is case-insensitive; the path need not contain the pattern) | #4 |
| `count_only` cap row contradicted §2.1 | `shown = total` in `count_only` mode; the row now reads `N matches` | #5 |
| Truncation described at the 100 KB layer | Two-layer reality documented: 16 KB `CONV_OUTPUT_CAP` fires first, drops the middle, footer survives, summary must be LAST | #6 |
| Context built from ALL hits (broke the 40-line standing order) | Context built from `hits[:preview_lines]` only — the previewed hits | #7 |
| Context format rule and example disagreed | One format, stated once: `  {rel}-{n}:{text}` (with colon), explicitly distinguished from grep's `-C` separator | #8 |
| "name matches first" ordering claim | Corrected: `sorted(ordered)` destroys it; the order is alphabetical | #9 |
| Summary omitted in `count_only`; `max_results` unreachable | Summary is the TRUE total over `grep_hits`, emitted in BOTH modes, with `(+K more files not listed)` | #10, #11 |
| `splitlines()` for context | `split("\n")` only — matches grep and `_start_byte_for_line`; form-feed divergence documented as pre-existing | #12 |
| No size guard on the context reader | Files over `MAX_READ_SIZE` (16 KB) are skipped for context; match lines still emit | #13 |
| Merge rule ambiguous at the "touch" boundary | Predicate stated as code: `if start <= prev_end + 1`; a touching-pair example added | #14 |
| §4 omitted the architecture file; §8 misnamed the path | §4 lists `.crabcakes/architecture.md`; §8 corrected; git-ignored status noted | #15 |
| Claimed the footer and indent are pinned | They are pinned NOWHERE — rev. 2 adds 2 new pin tests and corrects the risk column | #16 |
| `_search_files` docstring becomes false | §2.6 mandates the docstring update; the `file_search` parenthetical is dropped | #17 |
| `count_only` coercion unstated | `bool()` coercion stated explicitly, including `"false"` → `True` | #18 |
| `context` bool guard missing | Guard stated verbatim, excluding bools like the existing `preview_lines` guard | #19 |
| Schema delta unstated | Expected post-change sizes published (~coder 6430 / debugger 5995 / supervisor 5975) and the increase explicitly accepted | #20 |

---

## Code scope to probe

Unchanged from the pre-flight brief:

- `agent/tools.py` — `_search_files` (~962), `_run_grep` (~890), the `split(":", 2)` hit parser, the output emission loop (~1090-1119), `_file_search` (~1117), the `search_files` and `file_search` registrations (~1430-1479), the constants block (~270-285), `execute_tool` (~1808), `bound_tool_output` (~1742-1764)
- `agent/context.py` — `_find_matching_files` (956)
- `tests/test_tools.py` — `TestSearchFiles` (~589-660), `TestToolSchemaSizeBudget` (~1152)

---

## What to probe now

### A. Did the 20 corrections actually land, and are they now correct?
Go through the BUG list in the rev. 2 spec. For each, check that the fix is not merely present but **correct**. A fix that introduces a new error is worse than the original. Specifically verify:
- The `hits = grep_hits.get(rel) or []` guard does not break the existing `if hits:` truthiness branch.
- The `name_only` detection is correct: how does the implementation know a file was a name match? The spec says `rel in name_match_set` — check that such a set is actually available at that point in the code, or whether the spec needs to say it must be built.
- The `shown = total if count_only else len(hits[:preview_lines])` rule produces `40 of 41` in normal mode and `41 matches` in `count_only` mode — confirm no third path exists.
- The context merge predicate `start <= prev_end + 1` — check the boundary cases the spec lists (overlap at 8–12 / 11–15, touch at 8–12 / 13–17).
- The summary's population: summing over `grep_hits` while the emitted groups are capped at `max_results` — is the `(+K more files not listed)` computation coherent?
- The two-layer truncation: the summary is emitted LAST so it survives the 8 KB tail. Verify the tail actually contains the summary given the 2 KB head / 8 KB tail split.

### B. New bugs introduced by rev. 2
Rev. 2 is a substantial rewrite. Hunt for NEW problems, especially:
- The `count_only` mode omits the footer, but §2.3 says the summary is emitted in both modes and the footer follows the summary. Is the ordering consistent in `count_only` (summary last, no footer)?
- The `name_only` label: does it apply only when the file has NO content hits, or also to a file that is both a name match and a content match? The spec says name-match-only files get `name match`. Verify a file that is BOTH is labeled by its content count.
- The summary counts "matching lines across all of `grep_hits`" — but `grep_hits` only contains files reachable via grep. Name-only files are not in `grep_hits`. Does the summary's "files" count include name-only files? The spec does not say. This may be a NEW gap.
- The `context` size guard uses `MAX_READ_SIZE` (16 KB). Is 16 KB the right bound for a *context* read, or should it be larger? Does it create a surprising cliff (a 17 KB file gets no context, a 16 KB file does)?
- Phase 1 adds 10 tests and Phase 2 adds 12 — 22 new tests. Do any of them duplicate each other or assert the same thing twice?
- The spec's §5 implementation order: does each step have a verifiable exit condition? Step 4 says "verify the 40-line cap is unchanged" — is that actually a separate step or already covered by step 3's test?

### C. Spec-internal consistency
Rev. 2 changed many sections. Check the whole document for contradictions that survived the rewrite:
- §2.3 (summary) vs §3 step 8 (data flow) vs §6 (acceptance criteria) vs §7 (edge cases) — do all four agree on the summary's format, population, and the `(+K more files not listed)` marker?
- §2.1 says the `search_files` description is ~155 chars. Count it. Then check the §8 published post-change schema sizes against it — do they hold?
- §2.5's `test_search_context_zero_is_unchanged` compares post-Phase-1 to post-Phase-1. Is that test runnable in Phase 2 (after Phase 1 landed) as written?
- §6's acceptance criteria: are they all independently verifiable by a named test?

### D. Scope discipline
The spec must NOT implement the deferred items. Verify it does not:
- change `max_results` reachability
- fix the schema budget
- add a grep refusal
- change `_find_matching_files`
- change `agent/runtime.py`, `agent/context_strategy.py`, `agent/repo_map.py`

---

## Deliverable

Report in **BUG #[N]** format: Severity / Assumption violated / Attack vector / Reproduction / Root cause / Fix.

State explicitly **"no bugs found"** if the probe comes back clean.

If the spec is now implementable, say so explicitly and name anything still blocking. If it is not, name exactly which BUGs remain and what the spec must say.

Do not edit files. Do not commit. Report to the Supervisor only.
