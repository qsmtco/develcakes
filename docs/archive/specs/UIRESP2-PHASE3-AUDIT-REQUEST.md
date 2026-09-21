# PHASE 3 AUDIT REQUEST — UIRESP2 (FINAL AUDIT OF THE LOOP)

**To:** Debugger
**Mode:** `prompts/adversarialDebugger.md`, 11 sections — final audit; post-mortem follows your verdict.
**Spec contract:** `docs/specs/SPEC-UI-RESPONSIVENESS-2-PHASES-1-3.md` §2.3 (NOTE: §2.3.5 was AMENDED post-Phase-3 — see below) + §10.
**Scope:** `utils/feed_store.py` (`_is_pinned_card`, window branch in `compact_feed`, trigger wire-ups :589/:728), `ui/handlers/feed_handler.py` (`_persist_compactions` state, `_enqueue_compaction`, drain compactions phase, `_surface_prune_card`, `add_card(persist=)`, `_load_and_render` trigger), `tests/test_feed_store.py::TestWindowPruning` (5), `tests/test_feed_handler.py::TestWindowCompaction` (16 + 1 supervisor-added), `docs/ARCHITECTURE.md` (§3.22c/§3.22d/§4.14/§11 trees+inventory). Baseline c623196 → current 8749ecb.

## Supervisor actions taken (verify)

1. **Coder's flagged `_surface_prune_card` self-satisfying guard — CONFIRMED REAL, fixed by me directly** (my own spec bug; your r3 "guard-mitigated" verdict missed the derivation). The fix: `_ui` resolves the compacted project's NAME on the main thread by reverse lookup (`next(n for n,p in _project_paths.items() if p == project_path)`), assigns `card.project_name = name` there, then guards `if not name or _active_project_name != name`. Card's writer-side `project_name=""` placeholder. Spec §2.3.5 amended to match (with a fold-in note). Regression test added: `test_prune_card_not_misfiled_when_project_switched_mid_compaction` (A→B switch → nothing added + nothing persisted; A-still-active → card added, attributed to A by lookup).
2. **Commit hygiene note:** `8749ecb` inadvertently swept two pre-existing untracked supervisor drafts (`SPEC-AGENT-CONTROL-1.md`, `PROPOSAL-post-responsiveness-priorities.md`) via `git add -A`. Self-authored, on-topic, committed deliberately — no action needed; flagged for the post-mortem's process section.

## Probe list

1. **The fixed `_ui` reverse lookup:** two projects with the same path? (dict key collision — first match wins; impossible in practice since `_project_paths` is name→path keyed by open projects — verify). Reverse lookup cost O(projects) on main thread idle — trivial. The name resolution AFTER `idle_add` fires but BEFORE add_card — any TOCTOU vs project close between resolution and add_card? (Close clears `_active_project_name`; the guard read is atomic; residual window: resolved name matches, add_card runs, project closes concurrently mid-add — pre-existing exposure class, assess.)
2. **`_is_pinned_card` + window branch:** pins correct per §2.3.2 (accepted is not None / needs_review / needs_approval / git_commit)? `metadata or {}` — can metadata be None on a real card? Order preservation (chronological kept order — the `kept` list comprehension preserves index order — verify). pinned_kept count only counts outside-slice pins (display-only). `window=None` path intact. Zero/negative window edge (`window=0` → slice_start = len(cards) → all pinned kept — sane?). Return value semantics unchanged for Phase-2 callers.
3. **Triggers wired:** `update_feed_card` :728 + `_maybe_compact` :589 now `FEED_WINDOW_DEFAULT`; `_maybe_compact`'s rate-limit timestamp recorded BEFORE the size check (spec) — confirm still true post-edit. Load-time trigger `> FEED_WINDOW_DEFAULT * 1.25` in `_load_and_render` (background thread — confirm it's inside `_load_and_render`, not on the main thread).
4. **Drain compactions phase:** snapshot+clear at pass start; `compact_feed(path, window=FEED_WINDOW_DEFAULT)`; `if pruned: _surface_prune_card(...)`; failure: stop→ERROR drop; `tries+1>=3`→ERROR drop; else only-if-absent append `(path, tries+1)`; **external `_enqueue_compaction` REPLACE semantics** (fresh tries=0) — the r6#15 accepted cap-reset. `task`/`kind` init — Coder flagged omitting dead `kind` local (accepted; §10-note-3 intent preserved via `task`).
5. **`add_card(persist=)`:** all existing callers positional; `persist=False` skips the thread; the prune-card persist path (deepcopy + own thread) intact.
6. **Shutdown/loop final form:** `drained` + `queued_at_stop`/`leftover` now count `_persist_compactions`.
7. **Tests:** red-first evidence (3+15 failed pre-change); `TestSeqNumHandler` mock-constant fix (2 lines, `mock_fs.FEED_WINDOW_DEFAULT = FEED_WINDOW_DEFAULT` — the MagicMock comparison abort in the load thread); the new misfile test's complementary A-active case; AST structural test; RecordingGLib.
8. **ARCHITECTURE.md:** §3.22d/§4.14 vs the actual code — any doc-lies (deleted symbols documented, numbers verified — the Coder corrected stale inventory counts); the uniform-flock wording; file-inventory `feed-updates.jsonl`.
9. **Anything else the 11 sections surface.**

## Deliverable

BUG #[N] format, severity-tagged, traced. Explicit "no bugs found" per area. **Final verdict: ACCEPT / SEND-BACK** — the loop's exit condition (§7.2 of the loop prompt) requires your clean bill plus my verification before the post-mortem.
