# SPEC-44: search_files match counts and context lines

**Date:** 2026-10-20
**Author:** Supervisor (PM request 2026-10-20)
**Status:** Draft — for implementation (rev. 3, after two pre-flight audits)
**Implements:** PM request 2026-10-20 — "search_files: a match count, optional context lines, and the 40-line cap stays"
**Depends on:** SPEC-43 (coder harness cut) — inherits its `read_file` line-window contract and the `search_files` footer it introduced
**Target branch:** main
**Baseline:** HEAD `58464525` plus the uncommitted `read_file max_lines` change in the working tree.

> Architecture compliance: `agent/tools.py` is the owner of the `search_files` tool surface (ARCHITECTURE.md, "agent/ — the engine"). `agent/context.py` owns filename discovery (`_find_matching_files`). `agent/runtime.py` owns the send path and the tool loop. This spec changes the tool's output contract and its parameter schema. It does not add a package, a new tool name, or a new send path. The 40-lines-per-file preview cap is a **captain's standing order** (PM, 2026-10-20) and outranks this spec: it is preserved unchanged by both phases.

> **Revision 2** applies the pre-flight audit (Debugger, 20 BUGs, verdict "DO NOT IMPLEMENT AS WRITTEN"). The auditor's HIGH findings were independently re-verified by the Supervisor with standalone probes before being accepted; five of the twenty were re-tested and confirmed (BUG #1, #2, #6, #10, #16).
>
> **Revision 3** applies the re-audit (Debugger, 15 further BUGs, verdict "NOT YET IMPLEMENTABLE — 4 blocking findings"). The four blocking findings were independently re-verified by the Supervisor and all four reproduced: BUG #21 (`name_match_set` undefined → `NameError` on every search), BUG #22 (the `(+K more files not listed)` marker fails to fire while content files are dropped), BUG #24 (`split("\n")` phantom trailing element), BUG #27 (match line emitted twice / example contradicts the pinned format). Rev. 3 also fixes the non-blocking accuracy findings: #23 (name-only files in the summary), #25/#26 (measured schema sizes and description lengths), #28 (the 16 KB context cliff is a refusal, not a window), #29 (the "12 of 30" figure), #30 (the summary is emitted FIRST so it survives both truncation layers), #31 (four named Phase-3 summary tests), #32 (trailing empty line in `count_only`), #33 ("starts with" vs "contains"), #34 (per-step exit conditions), #35 (the `0 matches` branch is defensive). Every correction is marked inline with its BUG number.

---

## DISCOVERY

Read before any sample below was written.

- Read `agent/tools.py` `_search_files` (`962`): signature is `(pattern, project_path, path=None, file_type=None, max_results=20, preview_lines=40)`. Guards: empty pattern → `ToolResult(success=False, error="empty query")`; non-int or `<=0` `max_results` → reset to `20`; non-int or `<=0` `preview_lines` → reset to `40`; then `preview_lines = min(preview_lines, 40)`. The cap is applied at `985`, before any grep runs.
- **CORRECTED BY PRE-FLIGHT AUDIT (BUG #1):** `grep_hits.get(rel)` returns **`None`** for a file that is not a key of `grep_hits` — which includes every name-only match file. It is NOT an empty list. Any length operation on it raises `TypeError`. The existing emission loop relies on the truthiness check `if hits:` (`1108`), which is `False` for both `None` and `[]`. **Every count computation in this spec must guard for `None`.**
- **CORRECTED BY PRE-FLIGHT AUDIT (BUG #2):** an unforwarded parameter is **silently discarded** by the registration lambda's `**kwargs` catch-all — it does NOT raise `TypeError` and does NOT surface as a tool error. Verified by probe: `execute_tool("search_files", {"pattern": "def foo", "count_only": True}, proj)` returns `success=True`, `error=None`, and normal output with the parameter ignored. The `TypeError` catch in `execute_tool` fires only for a **missing required** argument (`execute_tool("search_files", {}, proj)` → `"missing 1 required positional argument: 'pattern'"`). Every registered tool lambda in `_TOOLS` has a `**kwargs` catch-all, so an unsupported argument can never raise from these handlers. There is no schema-validation layer in `agent/` that could reject an unknown key. **Consequence: a missed lambda edit is a silent wrong answer, not an error. The public-path test for each new parameter is load-bearing.**
- **CORRECTED BY PRE-FLIGHT AUDIT (BUG #10):** `max_results` is unreachable through `execute_tool` — it is swallowed by the same `**kwargs` sink. Probe: 30 matching files yield **20** groups via `execute_tool(..., {"max_results": 30})` and **30** groups via `_search_files(..., max_results=30)` called directly. The model cannot raise the file cap today. **This spec does not change that** (see §2 Scope), but it must account for it: `count_only` lists only the files that survived the 20-file default, so a per-file count list alone does not tell the caller how many files matched in total.
- **CORRECTED BY PRE-FLIGHT AUDIT (BUG #6):** `search_files` output is bounded by **two** layers, and the 16 KB one fires first. `_search_files` truncates at `MAX_EXEC_OUTPUT` (100 KB) as its last step, but `execute_tool` then calls `bound_tool_output`, which head/tail-bounds at `CONV_OUTPUT_CAP` (16 KB: 2 KB head + 8 KB tail) before the conversation. Probe: 30 files × 60 matching lines → final output 10,270 chars, `[... N chars omitted ...]` marker present, the 100 KB `[... truncated ...]` marker **never** produced, the footer **survives** in the tail, and most file groups are **dropped from the middle**. The exact number of visible groups varies with line length (probes measured 2–12 of 30 visible) — the meaningful fact is that the middle is dropped. **The dropped middle is exactly where the file groups and their counts live.** The new summary line is therefore emitted FIRST, so it survives the 2 KB head (BUG #30).
- **CORRECTED BY PRE-FLIGHT AUDIT (BUG #16):** the footer `[Open with read_file line=N only to edit.]` is pinned by **zero** tests, and the two-space indent before `Line {n}: ` is pinned by **zero** tests. Only the substring `"Line 3:"` is pinned (`test_tools.py:670`) and the exact output `"(no matches)"` is pinned at 3 sites. The spec's earlier claim that the footer and the indent are pinned was wrong. This spec adds those pins.
- Read `agent/tools.py` `_run_grep` (`~890`): one `subprocess.run(["grep", "-n", "-H", "--directories=skip", "-r", ...])` with `--` before the pattern. Flags are fixed: `-n -H --directories=skip -r`, plus `--include=*.<file_type>` when `file_type` is set. **There is no `-C`/`-A`/`-B` flag and this spec does not add one.**
- Read `agent/tools.py` hit parsing (in `_search_files`): `parts = line.split(":", 2)`; requires `len(parts) >= 3`; `fpath, lineno_s, content = parts`; `lineno` must parse as `int`; `rel = _project_rel_from_grep(fpath, root_rel)`; hits accumulate into `grep_hits: dict[str, list[tuple[int, str]]]`. The `split(":", 2)` form is why grep's `-C` output (which emits `path-lineno-content` and `path-lineno-content` context separators) is forbidden — it would not parse. **This spec does not change this parser.**
- Read `agent/tools.py` output emission (`1090-1119`): per file, `size_str` from `os.stat` (`?KB` on `OSError`; `size // 1024` + "KB" under 1MB, else `size // (1024*1024)` + "MB"); `lines.append(f"{rel} ({size_str})")`; then `for lineno, content in hits[:preview_lines]: lines.append(f"  Line {lineno}: {content.strip()}")`; when a file has no content hits, `lines.append("  [name match only — use read_file for content]")`. Then `lines.append("")` and `lines.append("[Open with read_file line=N only to edit.]")`. Then `if len(output) > MAX_EXEC_OUTPUT` (100 KB, `tools.py:281`) → `output[:MAX_EXEC_OUTPUT] + "\n[... truncated ...]"`.
- Read `agent/tools.py` `_file_search` (`1117`): the legacy name. Signature `(query, project_path, file_type=None, max_results=20, preview_lines=40)`; delegates to `_search_files(query, project_path, path=None, file_type=file_type, max_results=max_results or 20, preview_lines=preview_lines)`. It is registered as a tool but hidden from the API schema by `_API_SCHEMA_HIDDEN = frozenset({"file_search"})` (`~1774`) so the model sees `search_files` only. It exists so `execute_tool` can replay old transcripts.
- **CORRECTED BY PRE-FLIGHT AUDIT (BUG #17):** `file_search` shares the EMISSION path with `search_files` (both call `_search_files`). After Phase 1 its output also carries the count. Only its *parameters* are frozen. The spec's earlier parenthetical ("it has no count in its own call path") was false.
- Read `agent/tools.py` tool registration: `_TOOLS["search_files"]` is a `ToolDefinition` with `parameters.properties = {pattern, path, file_type}`, `required=["pattern"]`, `requires_approval=False`, and a handler lambda `lambda pattern, project_path, path=None, file_type=None, **kwargs: _search_files(pattern, project_path, path, file_type)`. **The lambda forwards only four arguments and swallows `**kwargs`; `max_results` and `preview_lines` are NOT reachable through the public `execute_tool("search_files", ...)` path.**
- Read `agent/tools.py` `execute_tool` (`~1808`): dispatches via `entry = _TOOLS.get(name)`; `args = dict(arguments)`; injects `project_path`, `session_key`, `approval_callback`, `scratch_dir`; then `result = handler(**args)`. The `allowed_tools` gate runs **before** the handler (`1848`). A `TypeError` from the handler is caught and returned as `ToolResult(success=False, error=f"Tool {name} received invalid arguments: {e}")` — but per BUG #2 that only fires for a MISSING required argument.
- Read `agent/context.py` `_find_matching_files` (`956`): signature `(project_path, query, patterns, max_files=20, max_total_chars=40_000, file_type=None, root_rel="")`. It returns **filename** matches only — a list of project-relative paths. It does not count content matches. Its `max_files` cap is independent of `_search_files`' `max_results`. **Audit note (BUG #9, free-form probe):** the `max_files` early return at `agent/context.py:1008` skips the size-sort at `:1009`, so when the cap fires the returned list is unsorted.
- Read `agent/tools.py` name-match path in `_search_files`: when `path` names a file, `name_matches` is `[_norm_search_rel(file_rel)]` if the pattern is a substring of the basename and the extension passes `file_type`; otherwise `[]`. When `path` is a directory, `name_matches` comes from `_find_matching_files(...)` filtered by `_under_search_root` and `_matches_file_type`. The name test is **case-insensitive** (`pattern.lower() in base.lower()`).
- **CORRECTED BY PRE-FLIGHT AUDIT (BUG #9):** name matches and content matches are merged into `ordered` as `[name_matches] + [grep_hits]`, but the very next line applies `sorted(ordered)`, which **destroys** the name-first order. The observed order is purely alphabetical by relative path. The spec's earlier DISCOVERY claim that name matches come first was wrong.
- Read `agent/tools.py` constants: `MAX_EXEC_OUTPUT = 100 * 1024` (`281`), `MAX_READ_SIZE = 16 * 1024` (`270`), `MAX_READ_LINES = 2000` (`277`), `LINE_WINDOW = 200` (`278`), `CONV_OUTPUT_CAP = 16 * 1024` (`285`). `read_file` is in `_ALREADY_WINDOWED`, so `search_files` output is head/tail-bounded by `CONV_OUTPUT_CAP` on its way into the conversation.
- Read `tests/test_tools.py` `TestSearchFiles` (`589-660`): `test_search_finds_match` asserts `"foo" in r.output`; `test_search_no_match` asserts `r.output == "(no matches)"`; `test_search_file_type_filter` asserts `"a.py" in r.output` and `"b.txt" not in r.output`; `test_search_file_type_filter_survives_name_cap` asserts `"zpkg/big.py" in filtered.output` and `".txt" not in filtered.output`, and its control case calls `_search_files("big", proj, path=".", file_type=None, max_results=30)` **directly**, with a comment stating the reason: "the search_files execute_tool registration swallows **kwargs and never forwards max_results, so the kwarg would be silently dropped and the default 20-cap would hide zpkg/big.py again."
- Read `tests/test_tools.py` `test_search_file_path_returns_the_line`: asserts `"tests/test_feed_card.py" in r.output` and **`"Line 3:" in r.output`**.
- Read `tests/test_tools.py` `test_search_file_path_miss_and_type_and_missing`: asserts `miss.output == "(no matches)"` (exact), `typed.output == "(no matches)"` (exact), and `missing.success is False` with `"Not a file or directory" in missing.error`.
- Read `tests/test_jit_context_discovery.py` `TestSearchFilesRef` (`427`): `test_search_files_unchanged_after_refactor` calls `_search_files("search_target", proj)` and asserts the behavior is identical post-refactor; `test_search_files_no_matches` calls `_search_files("totally_absent", proj)`; `test_search_files_is_the_only_search_schema` asserts `names.count("search_files") == 1` in the API schema.
- Read `tests/test_tools.py` `TestToolSchemaSizeBudget` (`~1152`): `TOOL_PAYLOAD_BUDGET = 4_000`; asserts `len(blob) < TOOL_PAYLOAD_BUDGET` for coder, debugger, and supervisor. **Measured at baseline HEAD `58464525`: coder 6155, debugger 5720, supervisor 5700 — all three already exceed the 4000 budget before this spec.** The uncommitted `read_file max_lines` change adds +94 chars to each (coder 6249, debugger 5814, supervisor 5794).
- Read `docs/specs/SPEC-43-CODER-HARNESS-CUT.md` §2.6: "Do not pass `-C` or `-A` to `_run_grep`. The parser at `1054` requires `path:lineno:content`." This spec inherits that prohibition and satisfies it by post-processing in Python.
- Read `.crabcakes/architecture.md` "agent/ — the engine": the module owns the turn engine, tool loop, context building, enforcement. The `search_files` tool surface lives in `agent/tools.py` with no documented convention about match counts or context lines. §8 of this spec adds one sentence. **Audit note (BUG #15):** the architecture file is `.crabcakes/architecture.md` and it is **git-ignored** (`.gitignore:37`) — an edit to it is invisible to the review layer and to the commit. There is no file named `ARCHITECTURE.md` at the repo root.

Architecture owner: `agent/tools.py` owns the `search_files` output contract and its parameter schema. `agent/context.py` owns filename discovery. `agent/runtime.py` owns the send path — unchanged by this spec.

Existing pattern to copy: the per-file group header `f"{rel} ({size_str})"` followed by indented `  Line {n}: {content}` entries. The new count is appended inside the existing header, and the new context lines are emitted inside the existing per-file block, in the same indentation style.

---

## 1. Overview

### Problem

`search_files` is the tool this project steers every agent toward instead of shelling out. Two gaps force a second tool call for the two most common needs:

1. **No match count.** To learn how many places use an API, the caller must count the returned lines by hand, or shell out to `grep -c` — the exact behavior this project has been trying to eliminate. Worse, the 40-line preview cap means the count cannot be recovered from the output at all when a file has more than 40 hits.
2. **No context lines.** A hit like `get(` does not say whether it is a definition or a call site. The caller must issue a follow-up `read_file` to find out.

### Solution

Two changes, in the order in section 5.

1. **Match counts.** Each file group header states the true number of matching **lines** in that file. A new `count_only` mode returns counts without any content lines.
2. **Context lines.** A new `context` parameter emits up to N lines before and after each match, post-processed in Python from the file's own content.

Both changes preserve the 40-lines-per-file preview cap exactly.

**Terminology (audit correction).** A "match" is a **matching line**, not a matching occurrence. A file with one line containing `foo` three times has **1** match, not 3. The count is the number of matching lines.

### Scope

| In | Out |
|---|---|
| Match count in the per-file group header | The 40-line preview cap (captain's standing order — unchanged) |
| `count_only` parameter | `max_results` reachability through `execute_tool` (pre-existing gap, not introduced here — see BUG #10) |
| `context` parameter, Python post-processing | `grep -C` / `-A` / `-B` flags (forbidden by SPEC-43 §2.6) |
| The `Line {n}: ` match-line format | The `  Line {n}: ` prefix itself (now pinned by a new test) |
| Both tool descriptions | `agent/context.py` `_find_matching_files` |
| Pinned footer and indent (new tests) | `agent/runtime.py`, `agent/context_strategy.py`, `agent/repo_map.py` |
| — | The schema-budget regression (separate unit; see §8) |

### Architecture principles

`agent/runtime.py` remains the only send path. No new package. No new tool name. The hit parser and its `split(":", 2)` contract are untouched.

---

## 2. Changes by File

### 2.1 `agent/tools.py` — match counts (Phase 1)

**The count must be the true total, not the number shown.** The preview cap slices `hits[:preview_lines]` at emission time; the count is computed from the full `hits` list before that slice.

Add a helper next to `_search_files` (`962`):

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
    file with zero hits (BUG #4).
    """
    if name_only:
        return f"{rel} ({size_str}, name match)"
    if total <= 0:
        return f"{rel} ({size_str}, 0 matches)"
    if shown < total:
        return f"{rel} ({size_str}, {shown} of {total} matches)"
    return f"{rel} ({size_str}, {total} matches)"
```

**The `None` guard (BUG #1) and the name-match test (BUG #21).** `grep_hits.get(rel)` returns `None` for a name-only file. The count MUST be computed as:

```python
hits = grep_hits.get(rel) or []      # None -> []
total = len(hits)
# `name_matches` is the existing local list in _search_files, assigned in
# BOTH the file-path and the directory branch and never rebound. It IS in
# scope at the emission loop. Use it directly:
name_only = (not hits) and (rel in name_matches)
```

**Do NOT invent a `name_match_set` identifier (BUG #21).** Rev. 2 of this spec did, and it was undefined — a literal implementation raised `NameError` on *every* search, because the expression is evaluated for the first file of every result set. `name_matches` is the real local; `rel in name_matches` is correct in all three paths (directory search, subdirectory-root search, explicit file path). If a set is wanted for O(1) lookup, it must be built explicitly before the emission loop:

```python
name_match_set = set(name_matches)   # optional, built BEFORE the emission loop
```

and the spec does not require it. `rel in name_matches` is the specified form.

Using `or []` normalizes `None` to `[]` and keeps the existing truthiness check (`if hits:`) working unchanged. **A literal `len(hits)` without this guard raises `TypeError` on the first name-match file of any search.**

**Name-match-only files (BUG #4).** A file whose only match is a filename match has no content hits. Its header reads `name match` — NOT `0 matches`. The name match is case-insensitive (`pattern.lower() in base.lower()`), so the displayed path does not necessarily contain the pattern; a `0 matches` header on a file that DID match would be a false signal. In `count_only` mode the `[name match only — use read_file for content]` body line is suppressed, so the header itself must carry the signal.

**The `0 matches` branch in the helper is defensive, not reachable (BUG #35).** By construction, a file in `ordered` is either a name match or a key of `grep_hits`, and `grep_hits` values are never empty. So `total == 0` implies `name_only` is True, and `name_only` is checked first — the `0 matches` branch is unreachable. It is kept as defense in depth. **The acceptance criterion for a name-only file therefore asserts the header TEXT (`name match`), not the absence of `0 matches`** — the latter would pass trivially and could not catch a regression in the `name_only` detection (exactly the regression BUG #21 would cause).

**`count_only` parameter.** New keyword on `_search_files`:

```python
def _search_files(
    pattern: str,
    project_path: str,
    path: str | None = None,
    file_type: str | None = None,
    max_results: int = 20,
    preview_lines: int = 40,
    count_only: bool = False,
) -> ToolResult:
```

`count_only` skips the content lines entirely: for each file, emit only the header. The `Line {n}: ` entries and the `[name match only ...]` line are omitted. The footer `[Open with read_file line=N only to edit.]` is omitted too — there is nothing to open.

**`count_only` and the preview cap (BUG #5).** In `count_only` mode no preview slice is applied at all, so `shown` is set to `total` and the header reads `N matches` — never `N of M matches`. Concretely: a 41-match file in `count_only` mode shows `41 matches`; the same file in normal mode shows `40 of 41 matches`. The implementation must therefore compute `shown` differently per mode:

```python
if count_only:
    shown = total
else:
    shown = len(hits[:preview_lines])
```

**Validation (BUG #18).** `count_only` is coerced with `bool(count_only)`. A JSON string `"false"` coerces to `True` under Python truthiness — this matches `list_files`'s `recursive` handling and is accepted for consistency; the schema declares `boolean` and the provider is expected to honor it. This is stated explicitly so a reviewer does not discover it later.

**The "(no matches)" path is unchanged.** When `ordered` is empty, `_search_files` returns `ToolResult(success=True, output="(no matches)")` before any header is built. `count_only` does not change that.

**Threading `count_only` through the tool registration (BUG #2).** The `search_files` lambda currently forwards only `(pattern, project_path, path, file_type)` and swallows `**kwargs` — an unforwarded parameter is **silently discarded**, not an error. It must forward `count_only` explicitly:

```python
lambda pattern, project_path, path=None, file_type=None, count_only=False, **kwargs:  # type: ignore
    _search_files(pattern, project_path, path, file_type, count_only=count_only),
```

**`file_search` does NOT get `count_only`.** It is hidden from the API schema (`_API_SCHEMA_HIDDEN`) and exists only for transcript replay. Adding the parameter there would be dead surface. **Its OUTPUT does change** — it shares the emission path — so the count appears in its results too (BUG #17).

**Descriptions (BUG #20).** The `search_files` description becomes — deliberately short, because the schema budget is already over:

```python
description=(
    "Find files by name or content. Per file: path, size, match count, "
    "up to 40 matching lines. count_only: counts only, no lines. "
    "Times out after 10s."
),
```

This is **146 chars versus the current 112** — a **+34** increase (measured, not estimated — rev. 2's "92 chars / +63" figures were wrong; the registered description is 112 chars). The count semantics are documented in the `count_only` property description instead of the tool description.

**Schema.** Add to `search_files` `parameters.properties`:

```python
"count_only": {"type": "boolean", "description": "Counts only, no lines. Header shows true total."},
```

### 2.2 `agent/tools.py` — context lines (Phase 2)

New keyword on `_search_files`:

```python
    context: int = 0,
```

**Validation and clamping (BUG #19).** The guard excludes bools explicitly, because `isinstance(True, int)` is `True` in Python. This mirrors the existing `preview_lines` guard at `tools.py:992`:

```python
if not isinstance(context, int) or isinstance(context, bool) or context <= 0:
    context = 0
context = min(context, MAX_CONTEXT_LINES)
```

With `MAX_CONTEXT_LINES = 3` defined next to the other constants.

**Context is produced in Python, never by grep.** The file is read from disk and the lines around each match are sliced. Do not add `-C`, `-A`, or `-B` to `_run_grep`; the hit parser's `split(":", 2)` requires `path:lineno:content`, and grep's context output does not have that shape (SPEC-43 §2.6).

**Implementation shape — with a size guard (BUG #13) and `\n`-only splitting (BUG #12).**

```python
def _file_lines_for_context(full_path: str, cache: dict[str, list[str] | None]) -> list[str] | None:
    """File lines for context slicing, cached per file. None if unreadable or too large.

    Splitting is on "\\n" ONLY. Python's ``str.splitlines()`` also splits on
    \\x0c, \\x1c, \\x1d, \\x1e, \\x85, \\u2028 and \\u2029, while grep counts
    only \\n and ``_start_byte_for_line`` counts only \\n. Using
    ``splitlines()`` here would desynchronize the context window from the
    ``Line N:`` numbers grep reported (BUG #12).
    """
    if full_path in cache:
        return cache[full_path]
    lines: list[str] | None = None
    try:
        if os.path.getsize(full_path) <= MAX_READ_SIZE:
            with open(full_path, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.read().split("\n")
    except OSError:
        lines = None
    cache[full_path] = lines
    return lines
```

- **Size guard (BUG #13, #28):** a file larger than `MAX_READ_SIZE` (16 KB) is **skipped for context** — the match lines still emit. Without this guard, a single 500 MB file that grep matched would be read entirely into the tool process.
  **The justification is a REFUSAL, not a window (BUG #28).** `_read_file` does NOT refuse large files — it refuses only above 50 MB and otherwise serves the first 16 KB **with a continuation notice**. The context reader serves **nothing** for a file over 16 KB. These are different policies and the spec does not claim they are the same.
  **Consequence, stated plainly:** on a real repository, `context` is unavailable for the largest files. In this repo, 7 of 27 `agent/*.py` files exceed 16 KB — including `agent/tools.py` (80 KB), `agent/runtime.py` (230 KB), `agent/context.py` (43 KB), `agent/enforcement.py` (71 KB), and `agent/context_strategy.py` (47 KB). A `search_files(pattern="def ", context=3)` here returns **no context for those files**. This is accepted for this spec; the alternative (serving a context window) is deferred.
- **`\n`-only split (BUG #12) with the trailing element dropped (BUG #24):** `split("\n")` keeps the Python line index aligned with grep's line numbers and with `_start_byte_for_line`. But `split("\n")` on a file ending with a newline yields a **trailing empty element** — `"l1\nl2\nneedle\n".split("\n")` is 4 elements for a 3-line file — which would produce a **phantom empty context line** past the end. That is the common case (most source files end with a newline), so the trailing element MUST be dropped:

```python
lines = content.split("\n")
if lines and lines[-1] == "":
    lines.pop()
```

  With that, the element count equals grep's line count for both file shapes. Note the pre-existing divergence this surfaces but does not fix: `read_file`'s `line=N` coordinate counts `\n` only, while `_read_file` renders with `splitlines(keepends=True)` — so in a form-feed file, `read_file(line=4)` and a `Line 4:` from `search_files` can point at different bytes. That is a pre-existing defect, out of scope here, but this spec must not widen it.

**Context is bounded to the PREVIEWED hits (BUG #7).** The context windows are built from `hits[:preview_lines]` — the same slice that produces the match lines — **not** from all hits. This is required to preserve the captain's standing order: the 40-line cap is a cap on the *preview* the model sees, and context lines are part of that preview.

The spec's earlier draft built windows from all hits, which for a 200-match file with `context=3` emitted up to 1,397 lines — the file's entire content — while the "40 match lines" assertion still passed. The cap was broken.

**Context emission — the match line is emitted ONCE (BUG #27, #38).** For a file with previewed hits `[(lineno, content), ...]` and `context = N`:

- **Build a lineno→content map FIRST (BUG #38).** The merged block is a range of line numbers over the FILE's lines, so the block loop has no access to grep's content unless the implementer builds this map. Without it, the obvious implementation renders EVERY line in the block from the file's raw line — including the match line — emitting the match in the dash format instead of the pinned `Line N:` format. The output still *looks* plausible (the difference is leading whitespace), but the pinned format is broken and the match-line count is wrong.

```python
content_by_lineno = {lineno: content for lineno, content in previewed_hits}
```

  Inside a block, for each line number `n` in the window:
  - if `n in content_by_lineno` → it is a MATCH line: `f"  Line {n}: {content_by_lineno[n].strip()}"`
  - else → it is a CONTEXT line: `f"  {rel}-{n}:{file_lines[n-1]}"` (the file's raw line)

- For each hit, the window is `lineno - N` through `lineno + N` (1-based, inclusive of the match line).
- Windows are **merged** when they overlap OR touch. The merge predicate is `if start <= prev_end + 1: merge`:
  - Overlapping pair: a hit at line 10 with `N=2` covers 8–12; a hit at line 13 covers 11–15 → merged block 8–15, emitted once.
  - Touching pair (adjacent, non-overlapping): a hit at line 10 with `N=2` covers 8–12; a hit at line 15 with `N=2` covers 13–17 → merged block 8–17, emitted once.
- **The match line is emitted exactly once, INSIDE the merged block.** The spec must not ALSO emit the match lines separately — reading §2.2 and §3 step 7 together previously implied emitting every match line twice.
- The match line uses **grep's stripped content**, matching the pinned format `f"  Line {n}: {content.strip()}"`.
- The surrounding context lines use the **file's raw line** (not stripped), in the dash format.
- **TOCTOU guard (BUG #43):** if the file shrank between the grep and the context read, a hit's line number can exceed the file's line count. Guard it: **if `lineno > len(file_lines)`, skip the context for that file** — its match lines still emit. Without the guard, `file_lines[lineno-1]` raises `IndexError`, which `execute_tool`'s broad handler converts into a tool error.

**The two content sources differ, and the spec pins which is which (BUG #27).** For a file line `    pattern: str,`:

| Source | Rendering |
|---|---|
| grep's content, stripped (the MATCH line) | `  Line 962: pattern: str,` |
| the file's raw line (a CONTEXT line) | `  agent/tools.py-962:    pattern: str,` |

They are **not** byte-identical. The match line keeps the existing `content.strip()` behavior; the context line preserves the file's leading whitespace.

**The context format, stated once (BUG #8).** The spec's earlier draft gave a rule and an example that disagreed, and mislabeled both as grep's convention. Corrected:

- **Our** context line format is `  {rel}-{n}:{text}` — e.g. `  agent/tools.py-961:def _search_files(`. It has a **colon** after the line number.
- **grep's** `-C` separator is `./f.txt-2-beta` — dash, line number, **no colon**. We are not copying grep's bytes; we are borrowing its idea that a dash marks a non-hit line.
- The **match** line stays `  Line {n}: {content.strip()}`.

A correct output block looks like:

```
  agent/tools.py-961:def _search_files(
  Line 962: pattern: str,
  agent/tools.py-963:    project_path: str,
```

(The match line shows grep's stripped content; the context lines show the file's raw lines.)

**Window edges.** A window that runs off the start of the file (`lineno - N < 1`) or past the end is truncated to the file's real bounds. No error, no padding, no negative line numbers.

**`context = 0` is byte-identical to today's output.** This is the regression guard for Phase 2: the file is not even read for context when `context == 0`. The comparison is **post-Phase-1 to post-Phase-1** (BUG #15) — Phase 1 changes the header, so a golden captured before Phase 1 would not match.

**`count_only` and `context` together:** `count_only` wins — it emits headers only, so no context lines are produced. The `context` value is ignored.

**Threading `context` through the registration lambda (BUG #3).** Phase 2 MUST extend the lambda that Phase 1 rewrote. Without this, `context` is silently discarded and the caller gets a wrong answer with no error:

```python
lambda pattern, project_path, path=None, file_type=None, count_only=False, context=0, **kwargs:  # type: ignore
    _search_files(pattern, project_path, path, file_type, count_only=count_only, context=context),
```

**Descriptions.** The `search_files` description gains the context clause (still short, BUG #20):

```python
description=(
    "Find files by name or content. Per file: path, size, match count, "
    "up to 40 matching lines. count_only: counts only, no lines. "
    "context: 1-3 lines around each match. Times out after 10s."
),
```

**184 chars** — a **+72** increase over the current 112.

**Schema.** Add to `search_files` `parameters.properties`:

```python
"context": {"type": "integer", "description": "Lines around each match, 0-3 (default 0)"},
```

### 2.3 `agent/tools.py` — summary line

Emit a one-line summary of the whole search:

```
[12 matches in 3 files]
```

**The population, with formulas (BUG #11, #23, #36, #37).** The summary counts BOTH kinds of match. The name-only population must be defined by a formula, not by prose — three readings of "the number of name-only match files" were all wrong:

```python
# A file that is BOTH a name match and a content match is counted ONCE,
# by its content. name_matches is the existing local list; grep_hits holds
# the content-hit files.
name_only_count = sum(1 for r in name_matches if not grep_hits.get(r))
total_matched   = len(grep_hits) + name_only_count
N = sum(len(v) for v in grep_hits.values()) + name_only_count   # matching LINES
M = total_matched                                                # matched FILES
K = total_matched - len(ordered)                                 # matched files NOT listed
```

- **Why not `len(name_matches)` (BUG #37):** a file that is both a name match and a content match would be counted twice. One file on screen would report `[2 matches in 2 files]`.
- **Why not the name-only files present in `ordered` (BUG #37):** that under-reports `M` whenever the `max_results` cap dropped name-only files — and the dropped ones are then reported nowhere.
- **The marker fires when `K > 0`, and `K` counts ALL matched files absent from the list** — content files and name-only files alike (BUG #36). A content-only `K` under-reports: with 10 name-only files sorted last and 20 content files, `max_results=20` drops the 10 name-only files, the content-only formula yields `K=0` (the marker never fires), and the summary reads `[30 matches in 30 files]` with no marker while 10 files are missing.

**The summary's arithmetic is self-consistent (BUG #36).** By construction `M == len(ordered) + K` in every case: the caller can subtract the headers it can see from `M` and get exactly `K`. A summary that says `M=30`, shows 20 headers, and marks `+5` is broken — the general formula above cannot produce that.

**A name-only-only search therefore reads `[3 matches in 3 files]`** — never `[0 matches in 0 files]`, which would contradict the `name match` headers directly above it.

**The name-only count is itself capped (BUG #42).** `_find_matching_files(..., max_files=max_results)` caps `name_matches` at 20. So `name_only_count` can never exceed `max_results`, and the true number of name matches in the project is **unknown to the code**. The summary reports the name matches the search FOUND, not all name matches that exist.

**Where the summary is emitted (BUG #30).** The output passes through TWO truncation layers: the 100 KB cut inside `_search_files` (keeps the HEAD, cuts the tail) and the 16 KB `bound_tool_output` head/tail bound (2 KB head + 8 KB tail, drops the middle). **Position alone cannot make the summary survive both** — with `context` enabled, 20 files × 40 previewed hits × 7-line windows is easily 240–300 KB, so the 100 KB layer becomes reachable in ordinary use and would cut a tail-positioned summary.

**The summary is therefore emitted FIRST** — as the very first line of the output, before the per-file groups. It then survives the 2 KB head of the 16 KB layer in every case, and the 100 KB layer (which keeps the head) also preserves it.

```
[12 matches in 3 files]
a.py (1KB, 2 matches)
  Line 1: def foo():
...

[Open with read_file line=N only to edit.]
```

**The summary is NOT omitted in `count_only` mode (BUG #10).** The spec's earlier draft omitted it on the reasoning that "every file's count is already listed" — that is false: `max_results` (20) truncates the list, and the 16 KB layer can drop more. The summary is precisely the aggregate that compensates for the truncation, so it is emitted in both modes.

**The trailing empty line in `count_only` (BUG #32, #39).** The current code appends `""` and then the footer as two separate `lines.append` calls. In `count_only` mode **both** the trailing empty line and the footer are omitted. The `count_only` output shape is: **the summary line first, then the file headers, and nothing after them.** The summary is NOT the final line in `count_only` — it is the FIRST line in every mode (BUG #39).

In normal mode the order is: summary first, then the groups, then the trailing empty line, then the footer.

The summary is omitted only when the result is `(no matches)`.

**No singular form (BUG #45).** The format is literal: `[1 matches in 1 files]`. There is no singular/plural handling. This is intentional — the format is machine-parseable and stable, and a variable format would complicate the model reading it.

### 2.4 `tests/test_tools.py` — Phase 1 tests

New tests in `TestSearchFiles` (`589`):

| Test | Asserts |
|---|---|
| `test_search_reports_match_count` | A file with 3 matching lines shows `3 matches` in the header |
| `test_search_reports_true_total_when_capped` | A file with 41 matching lines shows `40 of 41 matches` — N is 40, M is 41 |
| `test_search_count_only_returns_counts` | `count_only=True` shows the count and NO `Line ` entries and NO footer |
| `test_search_count_only_shows_true_total` | `count_only=True` on a 41-match file shows `41 matches`, NOT `40 of 41 matches` (BUG #5) |
| `test_search_count_only_name_match` | A name-only match shows `name match`, not `0 matches` (BUG #4) |
| `test_search_count_reaches_public_tool_path` | `execute_tool("search_files", {"pattern": ..., "count_only": True}, proj)` shows the count — proves the lambda forwards the parameter (BUG #2) |
| `test_search_count_preserves_line_format` | The `  Line {n}: ` format is unchanged (existing behavior preserved) |
| `test_search_footer_pinned` | The footer string is present — **new pin** (BUG #16) |
| `test_search_line_indent_pinned` | `"\n  Line 3: "` (with the two-space indent) is present — **new pin** (BUG #16) |
| `test_search_name_match_does_not_crash` | A search whose first result is a name-only match returns `success=True` — the BUG #1 guard |

The cap test needs 41+ matching lines in one file. Write a file with 41 lines of `def foo_N(): pass` and search for `def foo_`.

**The existing tests must not be weakened.** `test_search_no_match` asserts `r.output == "(no matches)"` — still true. `test_search_file_path_returns_the_line` asserts `"Line 3:" in r.output` — still true.

### 2.5 `tests/test_tools.py` — Phase 2 tests

| Test | Asserts |
|---|---|
| `test_search_context_zero_is_unchanged` | `context=0` output is byte-identical to the same search without the parameter — **both sides post-Phase-1** (BUG #15) |
| `test_search_context_shows_neighbours` | `context=2` shows the lines before and after a match, in the `  {rel}-{n}:{text}` format |
| `test_search_context_merges_overlapping_windows` | Two nearby matches produce one merged block, with no duplicated lines |
| `test_search_context_merges_touching_windows` | A hit at 10 and a hit at 15 with `N=2` merge into one 8–17 block (BUG #14) |
| `test_search_context_clamps_to_three` | `context=99` shows at most 3 lines on each side |
| `test_search_context_at_file_edges` | A match on line 1 and a match on the last line do not crash and do not pad |
| `test_search_context_never_parses_as_hit` | No context line **starts with** `  Line ` — the match format is a line prefix, not a substring (BUG #33) |
| `test_search_context_ignored_with_count_only` | `count_only=True, context=2` emits no context lines |
| `test_search_context_preserves_cap` | A file with 200 matching lines and `context=3` shows at most **40** match lines AND context only around those 40 — the total emitted lines must not approach the file length (BUG #7) |
| `test_search_context_skips_large_file` | A file larger than `MAX_READ_SIZE` (16 KB) gets no context lines, but its match lines still emit (BUG #13) |
| `test_search_context_reaches_public_tool_path` | `execute_tool("search_files", {"pattern": ..., "context": 2}, proj)` shows context — proves the lambda forwards it (BUG #3) |
| `test_search_context_uses_newline_split` | In a file containing `\x0c`, the context window lines match grep's line numbers (BUG #12) |

**Files NOT changed:**

- `agent/context.py` — `_find_matching_files` returns filename matches only; it has no content count to report and needs no change. (Its unsorted early-return at `:1008` is a pre-existing defect, out of scope.)
- `agent/runtime.py` — the send path and the tool loop are unchanged.
- `agent/context_strategy.py` — landing goal, `panic_prune`, dedup, pins: untouched.
- `agent/repo_map.py` — untouched.
- `agent/enforcement.py` — untouched.
- `utils/prompt_loader.py`, `prompts/system/*.md` — no prompt changes. The tool description carries the new parameters.
- `tests/test_jit_context_discovery.py` — `TestSearchFilesRef` asserts refactor-stability of `search_files`; the new params are additive and defaulted, so those tests still pass. Verify, do not edit.

### 2.6 `tests/test_tools.py` — Phase 3 tests (summary line)

**The summary is a headline feature with no coverage in rev. 2 (BUG #31).** These four tests are mandatory:

| Test | Asserts |
|---|---|
| `test_search_summary_reports_true_totals` | 30 matching files with `max_results=20`: the summary's file count is **30**, not 20 — the true total over `grep_hits` (BUG #11) |
| `test_search_summary_marks_unlisted_files` | The `(+K more files not listed)` marker is present AND `K` is the **correct** count of unlisted matched files — the fixture MUST mix name-only and content files so it would catch BUG #36, where a content-only `K` reported 0 while 10 files were dropped |
| `test_search_summary_emitted_in_count_only` | The summary is present in `count_only` mode (BUG #10) |
| `test_search_summary_counts_name_only_files` | A name-only-only search reads `[N matches in M files]`, NOT `[0 matches in 0 files]` (BUG #23) |

### 2.7 `agent/tools.py` — docstring update (BUG #17)

The `_search_files` docstring currently says: *"Each hit is path, size from `stat`, and at most 40 preview lines taken from the grep hit. **The hit file is not opened to count lines.**"*

That sentence becomes **false** the moment `context > 0` reads the file. Update it to:

> *"Each hit is path, size from `stat`, a match count, and at most 40 preview lines taken from the grep hit. The hit file is opened only when `context > 0` (and only if it is under `MAX_READ_SIZE`)."*

---

## 3. Data Flow

A `search_files` call after this spec:

1. `execute_tool("search_files", args, project_path)` → the `allowed_tools` gate → the `search_files` lambda → `_search_files(pattern, project_path, path, file_type, count_only=count_only, context=context)`.
2. `_search_files` validates `pattern`, `max_results`, `preview_lines`, `count_only`, `context`; clamps `preview_lines` to 40 and `context` to 3.
3. Path resolution: `_resolve_project_path` → file or directory → `grep_cwd` / `grep_target` / `root_rel`.
4. Name matches via `_find_matching_files` (directory) or the basename-substring test (file). Content matches via `_run_grep` → `grep_hits[rel] = [(lineno, content), ...]`.
5. `ordered` = name matches + content-hit files, merged then **sorted alphabetically** (the name-first order does NOT survive — BUG #9), capped at `max_results`.
6. If `ordered` is empty → `ToolResult(success=True, output="(no matches)")`.
7. **Summary line** — computed and emitted FIRST, before the per-file groups, so it survives both truncation layers (BUG #30, #39). It uses the §2.3 formulas, all of which are computable at this point because `ordered` (step 5) and `grep_hits` (step 4) are both known: `name_only_count = sum(1 for r in name_matches if not grep_hits.get(r))`, `total_matched = len(grep_hits) + name_only_count`, `N = sum(len(v) for v in grep_hits.values()) + name_only_count`, `M = total_matched`, `K = total_matched - len(ordered)` (BUG #36, #37). The marker `(+K more files not listed)` is appended when `K > 0`. The summary is emitted in BOTH modes (BUG #10).
8. Per file: `size_str` from stat; `hits = grep_hits.get(rel) or []`; `total = len(hits)`; `shown = total if count_only else len(hits[:preview_lines])`; `name_only = (not hits) and (rel in name_matches)`; header via `_match_count_header(rel, size_str, shown, total, name_only=name_only)`. If `count_only` → header only, next file. Else: if the file has content hits and `context > 0`, build `content_by_lineno = {lineno: content for lineno, content in previewed_hits}`, read the file's lines (cached, size-guarded, `\n`-split with the trailing empty element dropped), and emit merged context blocks — inside a block, a line number in `content_by_lineno` is a MATCH line rendered `f"  Line {n}: {content_by_lineno[n].strip()}"`, every other line is a CONTEXT line rendered `f"  {rel}-{n}:{file_lines[n-1]}"` (BUG #38), with the match line emitted ONCE (BUG #27); in the `context == 0` case, emit the match lines only, capped at `preview_lines`; else emit `  [name match only — use read_file for content]`. Skip the context for a file whose `lineno > len(file_lines)` (BUG #43).
9. Footer `[Open with read_file line=N only to edit.]` — omitted in `count_only`, along with the trailing empty line (BUG #32).
10. `if len(output) > MAX_EXEC_OUTPUT` → truncate with `\n[... truncated ...]`. This stays last inside `_search_files` and keeps the HEAD, so the summary (emitted first) survives it.
11. Back in `execute_tool`: `bound_tool_output` head/tail-bounds at 16 KB (2 KB head + 8 KB tail). The middle — where the file groups live — is dropped with a `[... N chars omitted ...]` marker. The summary survives in the 2 KB head; the footer survives in the 8 KB tail (BUG #6).

---

## 4. File Change Summary

| File | Change | Risk |
|---|---|---|
| `agent/tools.py` | counts, `count_only`, `context`, summary line, docstring, 2 descriptions, 2 schema props, 1 lambda | Medium. The `Line {n}:` format and the footer are pinned by NEW tests (they were not pinned before — BUG #16). |
| `tests/test_tools.py` | 10 Phase-1 tests, 12 Phase-2 tests, 4 Phase-3 tests | Low. |
| `.crabcakes/architecture.md` | one sentence (§8) — **git-ignored**, so invisible to the review layer (BUG #15) | Low. |

**Files NOT changed:** `agent/context.py`, `agent/runtime.py`, `agent/context_strategy.py`, `agent/repo_map.py`, `agent/enforcement.py`, `utils/prompt_loader.py`, `prompts/system/*.md`, `tests/test_jit_context_discovery.py`.

---

## 5. Implementation Order

Do these in order. A green step is the only reason to start the next one.

**Phase 1 — match counts.**
1. Add `_match_count_header` and the `count_only` parameter to `_search_files`; emit the header with the count; implement `count_only` (headers only, no footer); update the `_search_files` docstring (BUG #17). Use `rel in name_matches` — **not** an undefined `name_match_set` (BUG #21). **Exit condition (runnable):** `grep -n "name_matches" agent/tools.py` is pasted and shows the use inside the emission loop; `python -m py_compile agent/tools.py` is clean.
2. Thread `count_only` through the `search_files` registration lambda; add the schema property; update the `search_files` description. **Exit condition (runnable):** `grep -n -A2 "count_only" agent/tools.py` is pasted and shows `count_only` in BOTH the lambda signature and the `_search_files(...)` call — a missing forward is silently discarded (BUG #2), so the grep is the check, not a test that does not exist yet.
3. Add the 10 Phase-1 tests. Run `python -m pytest tests/test_tools.py -q` and paste the output. The 40-line cap is verified here by `test_search_reports_true_total_when_capped` (40 of 41). **Exit condition:** the pytest output is pasted and shows only the 3 pre-existing `TestToolSchemaSizeBudget` failures. `test_search_count_reaches_public_tool_path` is the check that the lambda forwards the parameter.

**Phase 2 — context lines.**
4. Add `MAX_CONTEXT_LINES = 3` and the `context` parameter; implement Python post-processing with the size guard, the `\n`-only split **with the trailing empty element dropped** (BUG #24), merged windows with the `prev_end + 1` predicate, the `content_by_lineno` map (BUG #38), and the TOCTOU guard (BUG #43). The match line is emitted ONCE, inside the merged block, with grep's stripped content (BUG #27). **Exit condition (runnable):** `python -m py_compile agent/tools.py` is clean and `grep -n "content_by_lineno" agent/tools.py` is pasted.
5. **Extend the registration lambda with `context` (BUG #3)** — a separate, explicitly-listed step because missing it produces a silent wrong answer. **Exit condition (runnable):** `grep -n -A2 "context=0" agent/tools.py` is pasted and shows `context=0` in the lambda signature AND `context=context` in the `_search_files(...)` call. This is the highest-risk step in the spec; the grep is the check, because the test that would catch it does not exist until step 7.
6. Add the `context` schema property; update the description. **Exit condition (runnable):** `grep -n -A6 '"context":' agent/tools.py` is pasted.
7. Add the 12 Phase-2 tests. Run `python -m pytest tests/test_tools.py -q` and paste the output. `test_search_context_reaches_public_tool_path` is the check that step 5's lambda edit landed. **Exit condition:** pasted output shows only the 3 pre-existing failures.
8. Grep sweep: `grep -n "cmd = \[\"grep\"" agent/tools.py` shows the flag list is unchanged — no `-C`, `-A`, or `-B` was added. **Exit condition:** the grep output is pasted.

**Phase 3 — summary line and final verification.**
9. Add the summary line: the §2.3 formulas (`name_only_count`, `total_matched`, `N`, `M`, `K`), emitted FIRST, in both modes, with the trailing empty line and footer omitted in `count_only` (BUG #32). Add the 4 Phase-3 tests (§2.6). **Exit condition:** `test_search_summary_reports_true_totals`, `test_search_summary_marks_unlisted_files`, `test_search_summary_emitted_in_count_only`, and `test_search_summary_counts_name_only_files` all pass. `test_search_summary_marks_unlisted_files` must use a fixture that MIXES name-only and content files so it would catch BUG #36.
10. Run the full affected suite: `python -m pytest tests/test_tools.py tests/test_jit_context_discovery.py -q` and paste the output. **Exit condition:** pasted output shows only the 3 pre-existing `TestToolSchemaSizeBudget` failures.
11. Run `python -m py_compile agent/tools.py`. **Exit condition:** pasted output.
12. Update `.crabcakes/architecture.md` (§8) — one sentence, in the git-ignored state dir. **Exit condition:** the sentence is present (read the file back).

---

## 6. Acceptance Criteria

- [ ] A search over a file with 3 matching lines shows `3 matches` in that file's header.
- [ ] A search over a file with 41 matching lines shows `40 of 41 matches` — the true total, not the shown count.
- [ ] `count_only=True` returns headers with counts, no `Line ` entries, and no `[Open with read_file line=N only to edit.]` footer.
- [ ] `count_only=True` on a 41-match file shows `41 matches`, not `40 of 41 matches`.
- [ ] A name-only match shows `name match` — never `0 matches`.
- [ ] A search whose FIRST result is a name-only match returns `success=True` (the `None` guard).
- [ ] `count_only=True` works through `execute_tool("search_files", {...})` — the parameter is forwarded by the registration lambda, not swallowed by `**kwargs`.
- [ ] The summary line reports the TRUE totals: `N` = matching lines across all of `grep_hits` **plus** the number of name-only match files; `M` = files with a content hit **plus** the number of name-only match files — not the number of headers emitted.
- [ ] The summary's arithmetic is self-consistent: `M == len(headers) + K` in every case — a caller can subtract the visible headers from `M` and get exactly the `K` the marker reports (BUG #36).
- [ ] The summary line is the **FIRST** line of the output in both modes (BUG #30, #39) — nothing precedes it.
- [ ] When more files matched than were listed, the summary carries `(+K more files not listed)`.
- [ ] The summary line is emitted in `count_only` mode too.
- [ ] `context=0` output is byte-identical to the same search without the parameter, compared post-Phase-1 to post-Phase-1.
- [ ] `context=2` shows the lines before and after a match, formatted `  {rel}-{n}:{text}` — a colon after the number, distinguishable from the `  Line {n}: {text}` match format.
- [ ] Two nearby matches with context produce ONE merged block with no duplicated lines; adjacent (touching) windows also merge.
- [ ] `context=99` is clamped to 3 lines per side.
- [ ] A match on the first line and a match on the last line produce no error and no padding.
- [ ] No context line **starts with** `  Line ` (the match format is a line PREFIX, not a substring — a context line containing the file's own source text `  Line {lineno}: ...` is legitimate, and searching this repo for `Line ` would produce one) (BUG #33).
- [ ] A 200-match file with `context=3` emits at most **40** match lines, and context only around those 40 — the total emitted lines stay far below the file length.
- [ ] A file larger than 16 KB gets no context lines, but its match lines still emit.
- [ ] A file whose last line ends with a newline produces NO trailing empty context line (the trailing element of `split("\n")` is dropped) (BUG #24).
- [ ] The match line is emitted exactly ONCE per match — not duplicated by the context block (BUG #27).
- [ ] `context` reaches the tool through `execute_tool("search_files", {"context": 2})` — the lambda forwards it.
- [ ] Context line numbers match grep's line numbers in a file containing `\x0c` (form feed).
- [ ] The 40-lines-per-file match cap is unchanged: a 41-match file shows exactly 40 match lines.
- [ ] `_run_grep`'s command still contains only `-n -H --directories=skip -r` (+ `--include` when `file_type` is set). No `-C`, `-A`, or `-B`.
- [ ] The existing test `test_search_file_path_returns_the_line` still passes — the `  Line {n}: ` format is unchanged.
- [ ] The existing test `test_search_no_match` still passes — `(no matches)` is still the exact output when nothing matches.
- [ ] NEW pins: the footer string and the two-space `  Line ` indent are asserted by tests.
- [ ] `grep -n "min(preview_lines, 40)" agent/tools.py` returns exactly one hit.
- [ ] The `_search_files` docstring no longer claims the hit file is never opened.
- [ ] No edit to `agent/context.py`, `agent/runtime.py`, `agent/context_strategy.py`, or `agent/repo_map.py`.

---

## 7. Edge Cases

| Case | Expected behavior |
|---|---|
| Zero matches anywhere | `"(no matches)"` — unchanged, no headers |
| A file with 0 content hits but a filename match | Header shows `name match`; body keeps `[name match only — use read_file for content]` |
| A file that is both a name match and has content hits | Count reflects the content matches; the name match is not separately counted |
| A file matched case-insensitively by name only | Header shows `name match` — the path does not contain the pattern, so `0 matches` would be a false signal |
| More than 40 matching lines in one file | Header shows `40 of N matches`; exactly 40 match lines emitted |
| `count_only=True` with more than 40 matching lines | Header shows `N matches` — no preview slice is applied, so `shown == total` |
| `count_only=True` and no path argument | Same as any search; `path` defaults to `.` |
| `count_only` with a non-bool truthy value | Coerced with `bool()`; no error raised. A JSON string `"false"` coerces to `True` (Python truthiness) — accepted for consistency with `list_files`'s `recursive` |
| `context` non-int or `<= 0` | Resets to 0 (no context), mirroring the `preview_lines` reset |
| `context` is a bool (`True`) | Excluded by the guard → resets to 0 (not treated as 1) |
| `context` above 3 | Clamped to 3 |
| `context` on a file with no content hits | No context emitted; the file is not read |
| `context` on a file larger than 16 KB | Context skipped for that file; the match lines still emit |
| `context` on a file that cannot be read | Context skipped for that file; the match lines still emit. `errors="replace"` handles undecodable bytes. |
| Two matching lines 3 apart with `context=2` | Windows overlap → one merged block |
| Two matching lines 5 apart with `context=2` (touching) | Windows adjacent → one merged block (8–17) |
| A match on line 1 with `context=3` | Window is lines 1–4; no negative line numbers, no padding |
| A match on the last line with `context=3` | Window is truncated at the file's last line |
| `count_only=True` and `context=2` together | `count_only` wins; no context lines |
| A file whose content is one very long line | One line; the match line is the whole file; context is empty |
| A file containing `\x0c` (form feed) | Context line numbers match grep's numbering (split on `\n` only) |
| `file_search` (the legacy hidden tool) | Parameters unchanged; OUTPUT also carries the count (it shares the emission path) |
| More than 20 files match (`max_results` cap) | Only 20 groups emitted; the summary line reports the true totals and appends `(+K more files not listed)` |
| A search that matched only by name | Summary reads `[N matches in M files]` — never `[0 matches in 0 files]` (BUG #23) |
| A mix of name-only and content files exceeds `max_results` | The `(+K more files not listed)` marker fires with `K = total_matched - len(ordered)` — matched files absent from the list, content AND name-only alike (BUG #36) |
| The same mix, with the name-only files sorted LAST so the cap drops them | The marker still fires: `K` counts dropped name-only files too. A content-only `K` would report 0 and never fire (BUG #36) |
| A file that is both a name match and a content match | Counted ONCE, by its content — `M` is 1, not 2 (BUG #37) |
| More name-only files exist than `_find_matching_files` returns | `name_matches` is capped at `max_results` (20) by the caller, so the summary reports the name matches FOUND, not all that exist (BUG #42) |
| A file shrank between the grep and the context read | The context is skipped for that file (`lineno > len(file_lines)`); its match lines still emit. No `IndexError` (BUG #43) |
| A file whose last line ends with a newline | `split("\n")` yields a trailing empty element, which MUST be dropped — no phantom empty context line past the end (BUG #24) |
| A file with no trailing newline | Unaffected — `split("\n")` yields the correct count |
| `context` on a file larger than 16 KB | Context skipped (a refusal, not a window); match lines still emit. On a real repo this means the largest files get no context (BUG #28) |
| Output over 100 KB inside `_search_files` | Truncated with `\n[... truncated ...]` — the cut keeps the HEAD, so the summary (emitted first) survives; the groups and footer are cut. Reachable in ordinary use once `context` is on (BUG #30) |
| Output over 16 KB on the public path | `bound_tool_output` head/tail-bounds: middle file groups (and their counts) are dropped with a `[... N chars omitted ...]` marker; the summary survives in the 2 KB head and the footer in the 8 KB tail (BUG #6) |

---

## 8. ARCHITECTURE.md Updates Required

Under "agent/ — the engine", after the file list, add one sentence to **`.crabcakes/architecture.md`** (the actual path; there is no root-level `ARCHITECTURE.md`):

> `search_files` reports a true per-file match count and can add bounded context lines around each match (SPEC-44). The 40-lines-per-file preview cap is unchanged — the count tells the caller how much was not shown.

No diagram change.

**Note (BUG #15):** `.crabcakes/architecture.md` is **git-ignored** (`.gitignore:37`). This edit is not visible to the review layer and is not part of the commit. It is listed in §4's file table for completeness only.

---

## Deferred — do not implement in this spec

- **The tool-schema budget regression.** All three special agents exceed the 4000-char budget at HEAD (coder 6155, debugger 5720, supervisor 5700), and the uncommitted `read_file max_lines` change adds +94 chars each (coder 6249, debugger 5814, supervisor 5794). **Measured** by mutating the real `ToolDefinition` and re-measuring the real payload: this spec's two schema properties add **+96** and the Phase-2 description adds **+72**, a total of **+254 per agent** → expected post-change **coder 6503 / debugger 6068 / supervisor 6048**. All three remain roughly 50% over the 4000 budget (BUG #20, #25, #26). The descriptions in this spec are deliberately short to limit the increase, but the fix itself is a separate unit: either trim descriptions or raise `TOOL_PAYLOAD_BUDGET` once the provider's real limit is confirmed.
- **A grep refusal for Supervisor and Debugger.** This is the lever that actually changes tool-choice behavior, but it must land AFTER Phase 1 — once grep is refused, `search_files` counts are the only way to count matches across many files. When it lands, the refusal text should redirect to `search_files(pattern=..., path=...)`.
- **Making `max_results` reachable through `execute_tool`.** It is swallowed by the `**kwargs` sink today (BUG #10). Bringing it into the schema would let the model raise the file cap — a genuine improvement, but it widens the public surface and belongs in its own unit.
- **The 17 ruff errors in `agent/tools.py`.** Byte-identical at HEAD; a separate mechanical unit.
- **The form-feed divergence between `search_files`' `Line N:` and `read_file`'s `line=N`.** Pre-existing, surfaced by BUG #12 but not widened by this spec. Separate unit.
- **A missing post-mortem for SPEC-43.** The coder harness cut shipped at `64fe637c` without one, which violates the loop's own exit condition. Backfill separately.
