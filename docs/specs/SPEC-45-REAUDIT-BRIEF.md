# SPEC-45 Re-audit Brief — fix round verification

**Auditor:** load `prompts/adversarialDebugger.md` fresh, all 11 sections, but
this pass is scoped: **verify the 7 findings are actually closed**, attack the
fixes themselves, and confirm no regressions. Full first-pass report is in the
team transcript; do not re-derive it.

**Base:** HEAD `985666f` + uncommitted fix round. First-pass tree SHA was
`75fbd4d3…`; tree has since changed (fix round). Re-derive the tree SHA
yourself and report it.

## Findings status table (what to verify)

| # | Finding | Claimed fix | Verify |
|---|---------|-------------|--------|
| 1 | feed_card missing from `ensure_builtin_tool` tuple (CRITICAL) | `"feed_card"` added at `utils/agent_defs.py:169-177`; regression tests in `tests/test_builtin_tool_migration.py` | tuple present; migration test drives `ensure_builtin_tool` directly (seed helper early-returns at `:144` without a src dir — the original test monkeypatched `_get_default_agents_src` to a nonexistent path and never reached the loop); tuple source-pin exists; LIVE INSTALL check: `get_special_agents()` shows feed_card in tools for Coder/Debugger/Supervisor (I verified True/True/True on this machine) |
| 2 | GTK batch-bar on tool-loop thread (HIGH) | `_update_batch_bar_for_active_project` moved inside `_append_html` closure (`feed_handler.py:448`→`449-452`); `:461` path wrapped in idle_add; 3 doc claims corrected (window.py:1347-1351, feed_handler.py:376, ARCHITECTURE.md:3240-3243) | both call sites hop to main; worker-thread test `test_add_card_defers_batch_bar_to_main_thread` exists and asserts no worker-thread GTK; docstrings now TRUE; check the closure move didn't break the HTML-path ordering (batch bar after card build) or leak late-binding bugs (`B023` loop-var capture is a known lint pattern in this file) |
| 3 | guard vs callback project-source divergence (MEDIUM) | `_post_feed_card` falls back to runtime handler's `_active_project[0]` when project handler returns None (window.py:1365-1374); both-None still rejects; project_name/tab_key divergence NOT unified | fallback present; divergence pin tests still pass; new tests `test_project_handler_none_falls_back_to_runtime_active_project` + `test_both_projects_none_still_fails` exist; adversarial: what happens when `_active_project` is set but the FEED isn't attached to that project, or when the runtime project and feed project differ — does the card land somewhere invisible? |
| 4 | add_cards_batch dead-code claims (MEDIUM) | comments corrected at feed_handler.py:477, ARCHITECTURE.md:3022/3024/4031; method retained | claims now say "no product callers since SPEC-45"; grep to confirm that is still true (no new caller appeared in the fix round); confirm no product caller existed at HEAD either |
| 5 | first-segment colon filter (LOW) | `":" in candidate` at group_tools.py:410; `a/C:/x.py` added to rejection matrix | any-colon check present; matrix includes the new path; sanity: does rejecting all colons break any LEGITIMATE project-relative path (e.g. filenames with colons on some filesystems)? Note trade-off in report |
| 6 | mutation-resistant forward test (MEDIUM) | minimum-args test asserts exact key set (7 fields + session_key, defaulted); new `test_registration_shape_matches_contract` pins the 7-param signature | both tests present and meaningful; re-run the Mutant B thought-experiment: would the suite still catch a dropped forward (the forward test retains value assertions — confirm) |
| 7 | red tripwire (LOW) | PM option (a): literal completed to 16 names; comment rewritten; test now GREEN | literal matches the actual 16-name API surface exactly (I verified the set); test passes; comment accurate |

## Out-of-scope for this pass (unchanged, already ruled)

- 125 ruff findings in legacy files — stash-compared byte-identical to HEAD,
  zero new debt. Do not spend budget.
- `TestToolSchemaSizeBudget` (3) and `test_chat_render_handler.py` (24)
  failures — pre-existing, byte-identical at HEAD.
- The bundled agent-context feature (context_edit.py etc.) — separate feature,
  will be split at commit time.

## Verification commands (evidence, not claims)

```bash
xvfb-run -a .venv/bin/python -m pytest tests/test_builtin_tool_migration.py \
  tests/test_spec24_facts.py tests/test_feed_card_tool.py tests/test_feed_handler.py -q
# expect 240 passed
```

Plus any probes you need. Probes to .debug/ and clean up after, as before.

## Verdict format

Per-finding: CLOSED / NOT CLOSED / REGRESSED (with evidence). Then overall
SHIP / FIX-FIRST. Report the tree SHA you derived.
