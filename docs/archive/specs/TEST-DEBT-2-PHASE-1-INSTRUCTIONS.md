# TEST-DEBT-2 Phase 1 — Instructions (Coder)

**Goal:** Reduce the 37-failure baseline. Every cluster below is triaged by
the Supervisor (2026-09-12, worktree-proven at 0d19b21). Triage findings are
authoritative — where this file says "test-side", fix the TEST; where it says
"product bug", fix the SOURCE. Flag anything that contradicts the triage.

Environment: GTK suites under `PYTHONDONTWRITEBYTECODE=1 xvfb-run -a python3
-m pytest <suite> -q`. pyflakes /tmp/pf-venv3. Hermetic (tmp_path / monkeypatch,
never real ~/.config). RED-first where you ADD a fix (behavioral tests).

## CLUSTER 1 — test_improve.py (11 failures) — TEST-SIDE (helper race)

Triage: `call_improve` helper (tests/test_improve.py:37) comments "GLib=None
so callback fires synchronously" — FALSE. `improve_prompt`
(utils/improve.py:191) ALWAYS dispatches its work via a daemon thread; GLib
only controls where the callback is *scheduled*. Tests assert before the
thread runs. Production path is correct (media_handler.py:79 passes GLib).

Fix: give the helper a bounded wait — e.g. `call_improve(..., timeout=5.0)`
polls `callback_store` (0.01s interval) after calling improve_prompt, asserts
exactly one entry. All 11 tests keep their existing assertions (content,
error-shape) — only the synchronization changes. If a test also asserts
callback_store CONTENT after the wait, that logic is unchanged.

RED evidence: run suite before fix (11F). GREEN: 13 passed (11 + the 2 that
already pass), in-suite, twice consecutively (flake gate).

## CLUSTER 2 — test_main_content_tab_switch.py (6) — TEST-SIDE (stale fixture)

Triage: fixture builds `MainContent.__new__` and stubs attributes, but
`_on_notebook_switch_page` now reads `self._tab_sessions` (added when
unread-tab tracking landed, ui/views/main_content.py:64). Fixture never sets
it → AttributeError ×6.

Fix: add `instance._tab_sessions = {}` to the `mc` fixture. Read
`_on_notebook_switch_page` (ui/views/main_content.py:640-ish) and add ANY
other attributes the current body reads that the fixture lacks (walk the code,
don't guess — run and iterate). Assertions unchanged.

## CLUSTER 3 — test_file_tree_columnview.py (3) — MIXED (verify, likely test-side)

Triage signals: `_on_tree_row_right_click` AttributeError (the test's
FakeFileTree no longer matches the real class surface — new attributes were
added to FileTree since); `Image` object has no `get_text` (a view element
changed from Label to Image — test asserts the wrong widget type now); `assert
None is True`. For each failing test: read the REAL current code
(ui/views/file_tree.py:2247 right-click; the icon/copy path), then decide
test-fix vs product-bug. The file's own docstring warns these tests are
mock-only and fragile — prefer re-anchoring the test to the real contract
over deleting coverage. If a REAL product regression is found (e.g. copy
path genuinely broken), STOP that test and flag it for a ruling.

## CLUSTER 4 — test_chat_terminal_segment (2) + test_chat_heading (1) — MIXED

Triage signals: `find_label_with` can't find `href="https://example.com"` in
any label of the built segment (link rendering moved/changed?), and an
isinstance(label) failure. Read ui/views/chat_bubble.py (or wherever terminal
segments + headings render), determine the real current contract (link label
shape, heading widget type), fix the TESTS to assert that contract. If links
are genuinely not rendered in terminal segments anymore, that is a product
regression — flag, don't fix silently.

## KNOWN NON-GOALS (leave red, do not touch)

- test_auxilium_tier2 (6), test_runtime_fallback (4), test_architecture (2),
  test_mcp_config (1), test_kb_integration (1) — separate units; documented
  pre-existing; several need product decisions.

## GATES

1. Clusters 1+2+4 fully green in-suite, twice consecutively.
2. Cluster 3: each of the 3 tests either green or flagged-with-evidence.
3. NO production source changes except where a cluster is ruled product-bug
   (expected: none; if any, separate commit with red-first proof).
4. Full-suite run: baseline 37F must drop by exactly the clusters fixed
   (target 37 − 11 − 6 − 3 − 2 − 1 = 14F if all green; report actual + the
   new failure list).
5. pyflakes 0 undefined on touched files. Hermetic throughout.
6. Commits: one per cluster (improve / tab-switch / file-tree / terminal+heading),
   conventional messages naming the cluster. Report COMPLETENESS per cluster
   with red/green evidence + the final full-suite failure list.

Then STOP. Debugger audits next.
