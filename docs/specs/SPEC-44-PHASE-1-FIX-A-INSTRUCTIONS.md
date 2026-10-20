# SPEC-44 Phase 1 — Fix A (test-only): pin the standing order and the count_only shape

**Builder:** Coder
**Supervisor:** Supervisor
**Trigger:** Phase-1 audit (Debugger) — verdict "implementation correct, zero product bugs; test suite does not back three of the spec's own acceptance criteria"
**Files to change:** `tests/test_tools.py` — **ONLY this file**
**Rules prompt:** `prompts/steelFramedCodeWriter.md`

**No product code changes.** `agent/tools.py` must be byte-identical after this fix. If you believe a product change is needed, stop and report instead.

---

## Why this fix exists

The Supervisor independently reproduced all three findings by mutation:

1. **The 40-line preview cap is pinned by ZERO tests.** Removing the slice (`hits[:preview_lines]` → `hits`) at `agent/tools.py:1142` — which makes `search_files` dump every match line and **breaks a captain's standing order** — left **155 tests passing** with only the 3 unrelated pre-existing budget failures. Verified.
2. **Four `count_only` behaviours are unpinned** (name-only body leak, trailing blank line, schema property removal, description revert).
3. **Two of the ten new tests are duplicates.**

---

## Fix 1 — pin the 40-line emission cap (the standing order)

Find `test_search_reports_true_total_when_capped` in `TestSearchFiles` and add ONE assertion to it, after the existing header assertion:

```python
            # The captain's standing order: at most 40 match lines EMITTED.
            # The header count is computed from the full hits list, so a header
            # assertion alone does NOT prove the cap holds — this does.
            assert r.output.count("  Line ") == 40
```

Also add this new test, which pins the cap on a much larger file where the header and the emission are far apart:

```python
    def test_search_emission_cap_40_on_large_file(self):
        """The 40-line emission cap holds even when the header reports a big total.

        Standing order (PM 2026-10-20): at most 40 match lines per file. The
        header's "N of M" is computed from the FULL hits list, so only counting
        the emitted "  Line " entries proves the cap. A mutant that drops the
        `hits[:preview_lines]` slice at the emission site passes every header
        assertion while dumping the whole file.
        """
        with tempfile.TemporaryDirectory() as proj:
            with open(os.path.join(proj, "big.py"), "w") as f:
                f.write("".join(f"def foo_{i:03d}(): pass\n" for i in range(120)))
            r = execute_tool("search_files", {"pattern": "def foo_"}, proj)
            assert r.success is True
            assert "40 of 120 matches" in r.output
            assert r.output.count("  Line ") == 40
```

## Fix 2 — pin the exact `count_only` output shape

Add these tests to `TestSearchFiles`:

```python
    def test_search_count_only_output_is_exact(self):
        """count_only emits ONLY the header: no body, no footer, no blank, no newline."""
        with tempfile.TemporaryDirectory() as proj:
            with open(os.path.join(proj, "counts.py"), "w") as f:
                f.write("def foo():\n    pass\n")
            r = execute_tool("search_files", {"pattern": "foo", "count_only": True}, proj)
            assert r.success is True
            # Size-agnostic: the whole output is one line, ending in the count.
            assert r.output.splitlines() == [r.output]
            assert r.output.endswith("1 matches)")
            assert not r.output.endswith("\n")
            assert "Open with read_file" not in r.output
            assert "  Line " not in r.output
            assert "\n" not in r.output

    def test_search_count_only_name_only_output_is_exact(self):
        """A name-only file in count_only mode emits the header and NOTHING else.

        Pins the `if count_only: continue` guard: without it the
        "[name match only ...]" body line leaks, and it contains no "Line "
        so a substring assertion on "Line " cannot catch it.
        """
        with tempfile.TemporaryDirectory() as proj:
            with open(os.path.join(proj, "needle.py"), "w") as f:
                f.write("x = 1\n")
            r = execute_tool("search_files", {"pattern": "needle", "count_only": True}, proj)
            assert r.success is True
            assert "name match)" in r.output
            assert "name match only" not in r.output
            assert r.output.splitlines() == [r.output]   # single line, no blank after

    def test_search_count_only_is_in_the_schema(self):
        """The model must be able to DISCOVER count_only."""
        defn = next(t for t in get_all_tools() if t.name == "search_files")
        assert "count_only" in defn.parameters["properties"]
        assert defn.parameters["properties"]["count_only"]["type"] == "boolean"

    def test_search_description_advertises_the_count(self):
        """The description must tell the model the count and count_only exist.

        Without this, the feature ships invisible: the model never learns to use it.
        """
        defn = next(t for t in get_all_tools() if t.name == "search_files")
        assert "count" in defn.description.lower()
        assert "count_only" in defn.description
```

**Check the import.** `get_all_tools` must be importable in `tests/test_tools.py`. Look at the existing imports at the top of the file — if `get_all_tools` is not already imported, add it to the existing `from agent.tools import ...` line. Do not add a second import statement if one already covers it.

## Fix 3 — de-duplicate the two overlapping tests

`test_search_count_only_returns_counts` and `test_search_count_reaches_public_tool_path` build the identical fixture and make the identical call. Per the audit, the second adds nothing the first does not assert.

**Keep both names** (they document different intents), but make the second genuinely test the public path in a way the first does not — prove the *lambda* forwards the parameter rather than the function default being used:

Rewrite `test_search_count_reaches_public_tool_path` to contrast the two paths:

```python
    def test_search_count_reaches_public_tool_path(self):
        """The registration lambda must FORWARD count_only to _search_files.

        The lambda's `**kwargs` sink silently discards an unforwarded parameter,
        so without the forward this call returns content lines and no count
        difference — no error is raised. This test calls the same function
        directly as the CONTROL, proving the public path and the direct path
        agree.
        """
        from agent.tools import _search_files

        with tempfile.TemporaryDirectory() as proj:
            with open(os.path.join(proj, "a.py"), "w") as f:
                f.write("def foo():\n    pass\n")
            via_tool = execute_tool(
                "search_files", {"pattern": "foo", "count_only": True}, proj
            )
            direct = _search_files("foo", proj, path=".", count_only=True)
            assert via_tool.success is True
            assert direct.success is True
            # Both paths must produce the same count_only output.
            assert via_tool.output == direct.output
            assert "  Line " not in via_tool.output
            assert "Open with read_file" not in via_tool.output
```

---

## Verification you must paste

```
.venv/bin/python -m py_compile tests/test_tools.py
.venv/bin/python -m pytest tests/test_tools.py::TestSearchFiles -q
.venv/bin/python -m pytest tests/test_tools.py -q
git diff --stat
```

**Expected:**
- `TestSearchFiles` all green (21 tests: 16 existing + 5 new minus the rewritten one = check the count yourself and report it).
- Full file: **3 failed, 97 passed** — the 3 are `TestToolSchemaSizeBudget` (pre-existing at HEAD, budget untouched).
- `git diff --stat` for this fix must show **`tests/test_tools.py` only**. `agent/tools.py` must NOT appear. If it does, you have made a product change — revert it and say so.

**Then prove the new tests actually bite.** Apply this temporary mutation, run the suite, confirm a test FAILS, then revert the mutation:

```
# mutate: agent/tools.py:1142  hits[:preview_lines]  ->  hits
.venv/bin/python -m pytest tests/test_tools.py -q     # MUST show a NEW failure
# revert, then re-run to confirm green again
```

Paste both runs. This is the acceptance evidence that the standing order is now pinned. **Leave the file reverted and unmutated.**

---

## COMPLETENESS checklist — mandatory

```
COMPLETENESS:
- [x/not done] Fix 1: cap assertion added to test_search_reports_true_total_when_capped — <line>
- [x/not done] Fix 1: test_search_emission_cap_40_on_large_file added — <line>
- [x/not done] Fix 2: test_search_count_only_output_is_exact added — <line>
- [x/not done] Fix 2: test_search_count_only_name_only_output_is_exact added — <line>
- [x/not done] Fix 2: test_search_count_only_is_in_the_schema added — <line>
- [x/not done] Fix 2: test_search_description_advertises_the_count added — <line>
- [x/not done] Fix 3: test_search_count_reaches_public_tool_path rewritten to contrast both paths — <line>
- [x/not done] TestSearchFiles green — paste output
- [x/not done] full test_tools.py = 3 pre-existing budget failures only — paste output
- [x/not done] git diff --stat shows tests/test_tools.py ONLY (agent/tools.py absent)
- [x/not done] MUTATION PROOF: cap mutant produced a NEW failure, paste it; then reverted and green, paste it
```

**Scope lock:** `tests/test_tools.py` only. No product code. No `TOOL_PAYLOAD_BUDGET` change. No `file_search` description change (that item is deferred). No Phase-2 work.
