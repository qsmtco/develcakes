# SPEC-04 Sub-Phase 4 Instructions — Legacy Test Files + Full-Suite Green

**Spec:** docs/specs/SPEC-04-R5-AUXILIUM-REMOVAL.md §2 "Tests needing edits" + §5.5/5.6
**Scope: the KB-referencing test files + `tests/test_no_kb_residuals.py` if needed.**
NO source files (SP3's sweep is zero; keep it that way — any source edit here is a bug).

## The files (14 with KB refs + 1 broken fixture)

From the 2026-09-22 survey grep (re-verify — some may have zero references now):
test_agent_builder_dialog, test_agent_builder_fallback, test_agent_builder_handler,
test_agent_config_yaml_fallback, test_agent_defs, test_bug_fixes, test_mcp_integration,
test_provider_test, test_runtime_caller_resolution, test_settings_dialog,
test_settings_handler, test_special_agents (the 5 fixture errors — see below),
test_config_invalidation (may have zero — check).

**test_special_agents.py fix (SP3 hand-off, highest priority):** the
`supervisor_def_present` fixture (:279-280) does `shutil.copy2(prompts/default_agents/
auxilium.yaml, …)` → FileNotFoundError. Delete: the aux copy + its teardown + the 2
dead aux test methods (`test_auxilium_not_auto_added`, `test_auxilium_auto_open_still_true`).
The 3 real supervisor tests must go green.

## Rules

1. **Delete only KB/auxilium-specific test cases** (local-kb seeding, KB fallback,
   auxilium wizard, kb_provider_registration). Everything else stays — a test that
   merely mentions "local-kb" in a comment gets a comment fix, not deletion.
2. **Never weaken an assertion** to make a test pass. If a non-KB test fails post-SP3,
   that's a REGRESSION to investigate and report — not to paper over.
3. GTK-gated files (test_settings_dialog, test_agent_builder_dialog): if they can't
   run headless in this env (banked segfault class), make the minimal KB-reference
   cleanup and verify via `--collect-only` + grep instead of a run. Say which files
   got which treatment.
4. If any test's PURPOSE was KB behavior (whole class/file), delete it and say so —
   like test_runtime_fallback in SP2 (adjudicated precedent).

## Final gates (the SPEC-04 §6 acceptance run)

```
.venv/bin/python -m pytest tests/ -q -p no:cacheprovider --ignore=tests/test_feed_card.py --ignore=tests/test_feed_handler.py 2>&1 | tail -5
```
(The two ignored files are the banked clean-HEAD gi/cairo segfaults — same exclusions
the testing phase will face. If OTHER files segfault, report, don't silently add.)

```
.venv/bin/python -m pytest tests/ --collect-only -q 2>&1 | tail -2          # 0 errors
grep -rin "auxilium\|kb_server\|kb_lookup\|KB_OUT_OF_SCOPE" agent/ ui/ utils/ models/ main.py tests/ --include="*.py" | grep -v test_no_kb_residuals | wc -l   # expect small residue ONLY in comments/pins, report each
.venv/bin/python -m ruff check tests/ 2>&1 | tail -1                        # vs pre-SP4 baseline (measure first)
.venv/bin/pyright tests/test_no_kb_residuals.py 2>&1 | tail -1
```

## COMPLETENESS
- [ ] test_special_agents green (fixture fixed, 3 supervisor tests pass)
- [ ] Per-file treatment table (edited / cases-deleted / file-deleted / comment-only)
- [ ] Full-suite number (X passed, Y failed — and WHY for every failure: pre-existing
      banked vs regression; regressions get investigated, not waived)
- [ ] Residue grep count + justification per hit
- [ ] ruff baseline comparison on tests/
- [ ] Deviations flagged
