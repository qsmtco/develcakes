# Implementation Phase I.5 — Tests

**Spec:** `docs/specs/SPEC-PROJECT-SETTINGS-BAR-ENHANCED-FIX-3.md` §5 (Step 2 + Step 3 + Step 5 regression tests)
**Prompt to load:** `prompts/steelFramedCodeWriter.md`
**Working dir:** `/path/to/projects/crabcakes`

## Task

Add automated regression tests for the new code from Phases I.2, I.3, and I.4. The implementation works (Debugger's ad-hoc probes all passed) but lacks persistent test coverage. Three test gaps were flagged:

1. **Phase I.2 gap:** FeedHandler round-trip + warning gate (spec §5 Step 2)
2. **Phase I.3 gap:** MainContent gear-preservation (spec §5 Step 3)
3. **Phase I.4 gap:** Window integration — branch worker token/cache/lifecycle (Debugger BUG #6)

## Files to create/modify

**3 test files:**
1. `tests/test_feed_handler.py` — ADD tests for `get_auto_accept_level` / `set_auto_accept_level` / `_commit_auto_accept_level` (append to existing file if it exists; the existing GTK-dependent tests segfault in sandbox — your new tests must be pure-Python, no GTK)
2. `tests/test_main_content_settings_bar.py` — NEW file. Gear-preservation + `_clear_settings_bar` + `update_project_settings` logic tests (use fakes/mocks for Gtk, NOT real widgets — sandbox segfaults on real GTK construction)
3. `tests/test_window_settings_bar.py` — NEW file. Window integration tests for branch worker token/cache/lifecycle (mock `_project_handler`, `_feed_handler`, `_main_content`)

## CRITICAL constraint: GTK segfaults in this sandbox

The Debugger and Coder both confirmed GTK widget construction segfaults in this sandbox (`Gtk.Label()`, `Gtk.Button()`, etc.). **Your tests MUST NOT construct real GTK widgets.** Use one of:
- **Fake/mock Gtk classes** (like the Coder's verification harness did in I.3 — swap the module-global `Gtk` for lightweight fakes)
- **`unittest.mock.MagicMock`** for any Gtk-dependent attribute
- **Direct method testing** with manually-injected fakes for `_project_settings`, `_settings_btn`, etc.

If a test requires real GTK, mark it `@pytest.mark.skip(reason="GTK segfaults in sandbox — environmental")` and note it. The pure-Python logic tests are the priority.

## Test specifications

### A. `tests/test_feed_handler.py` — FeedHandler auto-accept level (spec §5 Step 2)

Add a test class `TestAutoAcceptLevel` (or similar) with:

1. **`test_round_trip_all_four_levels`** — set each of off/diffs/files/all, get it back, assert round-trip. Use a real `AutoAcceptPrefs` (pure Python, no GTK).
2. **`test_distinct_states_persisted`** — set each level, serialize via `to_dict()`, deserialize via `from_dict()`, assert the level is preserved.
3. **`test_invalid_level_noop`** — `set_auto_accept_level("bogus")` is a no-op (state unchanged).
4. **`test_off_path_emits_callback`** — set `"off"`, assert `_emit_auto_accept_level_changed` fired with `"off"`.
5. **`test_warning_gate_on_enable`** — wire a mock `_show_auto_accept_warning`; call `set_auto_accept_level("files")`; assert warning was invoked with category `"files"`; assert level is STILL `"off"` (not committed yet); call `on_confirm`; assert level committed to `"files"` and `_refresh_auto_accept_state` called.
6. **`test_warning_cancel_no_commit`** — same setup; call `on_cancel`; assert level NOT committed (stays `"off"`); assert NO emit fired.
7. **`test_off_bypasses_warning`** — `set_auto_accept_level("off")` from any state does NOT invoke the warning (off is always safe).
8. **`test_exec_untouched`** — after any set/commit, `exec_command` auto-accept mode is unchanged.
9. **`test_prefs_none_guard`** — with `_prefs = None`, all 4 levels are no-ops.
10. **`test_refresh_called_after_commit`** — spy on `_refresh_auto_accept_state`; assert call count after each commit path.

### B. `tests/test_main_content_settings_bar.py` — MainContent bar logic (spec §5 Step 3)

Use fake Gtk classes (swap the module global) so the real method bodies execute. Tests:

1. **`test_clear_settings_bar_sibling_walk`** — populate `_project_settings` with fake children; call `_clear_settings_bar()`; assert all removed via `get_first_child`/`remove` loop (NOT `list()`).
2. **`test_update_project_settings_hides_on_empty`** — call `update_project_settings("", 0, None, "off", None)`; assert `set_visible(False)` called and bar cleared.
3. **`test_update_project_settings_shows_on_nonempty`** — call with valid args; assert `set_visible(True)` and info_box + gear appended.
4. **`test_gear_preserved_in_set_project_settings_text`** — call `set_project_settings_text("x")`; assert `_settings_btn` still has a parent (was re-appended after clear).
5. **`test_gear_preserved_in_set_feed_bar_text`** — same for `set_feed_bar_text("y")`.
6. **`test_xml_escape_for_project_name`** — call with project_name `"<b>injected</b>"`; assert the markup passed to the label contains `&lt;b&gt;` (escaped), NOT raw `<b>`.
7. **`test_xml_escape_for_branch`** — call with branch_name `"<script>"`; assert escaped in markup.
8. **`test_resolve_agent_display_name_fallback`** — no `_agent_mgr`, no `_agent_runtime_handler`; assert returns session_key as-is.
9. **`test_resolve_agent_display_name_empty_value_fallback`** — `_agent_runtime_handler.get_special_agents()` returns `{"special:x": ""}`; assert returns `"special:x"` (not `""`) — Round 3 BUG #7.
10. **`test_click_handlers_guard_none`** — call each click handler with its callback set to None; assert no-op (no AttributeError).

### C. `tests/test_window_settings_bar.py` — Window integration (Debugger BUG #6)

Mock `_project_handler`, `_feed_handler`, `_main_content`. Tests (use the Debugger's 7 scenarios as a guide):

1. **`test_branch_worker_scheduled_on_cache_miss`** — cold open; assert `_schedule_branch_refresh` started a worker (token set).
2. **`test_branch_result_discarded_on_token_mismatch`** — schedule for token 1; bump token to 2; call `_on_branch_result(1, ...)`; assert cache NOT written.
3. **`test_branch_result_discarded_on_path_mismatch`** — schedule for path /a; call `_on_branch_result(token, /b, ...)`; assert discarded.
4. **`test_branch_result_discarded_on_active_project_mismatch`** — schedule for project A; switch active to B; call `_on_branch_result(token, /a, "A", ...)`; assert discarded.
5. **`test_cache_hit_skips_scheduling`** — populate `_cached_branch_by_path["/a"] = "main"`; call `_on_feed_bar_update("A", 2)`; assert NO new worker scheduled.
6. **`test_project_closed_invalidates_in_flight`** — schedule worker; call `_on_project_closed("A")`; assert token bumped and in-flight cleared.
7. **`test_project_opened_retriggers_update`** — Round 4 build-time fix: call `_on_project_opened("A", "/a")`; assert `_on_feed_bar_update` was called.
8. **`test_solo_target_validation_rejects_unknown_project`** — `set_solo_target("nonexistent", "x")`; assert no-op.
9. **`test_solo_target_noop_on_same_value`** — set same value; assert callback NOT fired.
10. **`test_autoaccept_cycle_no_optimistic_rebuild`** — call `_on_autoaccept_cycle_clicked("off")`; assert `_on_feed_bar_update` NOT called (bar updates via callback after confirm).

## Rules

- Use `steelFramedCodeWriter.md` prompt
- Read the existing test files first to match conventions (`tests/test_feed_handler.py`, `tests/test_project_handler.py` for patterns).
- **Pure Python only.** No real GTK widget construction. Use fakes/mocks.
- Each test must be independent (no shared mutable state across tests).
- Use `pytest` conventions (`assert` statements, `unittest.mock` for mocks).
- Run `python3 -m pytest <test_file> -v` and paste the output.

## Verification (paste output in COMPLETENESS)

1. **`python3 -m pytest tests/test_feed_handler.py::TestAutoAcceptLevel -v`** (or whatever class name you use) — all new tests pass.
2. **`python3 -m pytest tests/test_main_content_settings_bar.py -v`** — all pass.
3. **`python3 -m pytest tests/test_window_settings_bar.py -v`** — all pass.
4. **`python3 -m pytest tests/test_project_handler.py -q`** — confirm 35/35 still pass (no regression).
5. **`python3 -m pytest --co -q`** — confirm full collection count increased by the number of new tests.

## COMPLETENESS checklist (required)

```
COMPLETENESS:
- [x] A: Added FeedHandler tests (10 tests) — evidence (test count + pytest output)
- [x] B: Added MainContent tests (10 tests) — evidence
- [x] C: Added Window integration tests (10 tests) — evidence
- [x] No real GTK widgets constructed (all fakes/mocks) — evidence
- [x] pytest test_feed_handler new tests pass — output
- [x] pytest test_main_content_settings_bar pass — output
- [x] pytest test_window_settings_bar pass — output
- [x] pytest test_project_handler 35/35 still pass — output
- [x] Full collection count increased — output
```

Report back with COMPLETENESS + verification evidence. Please write when done.
