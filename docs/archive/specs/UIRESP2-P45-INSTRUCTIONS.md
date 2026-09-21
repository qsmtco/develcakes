# UIRESP2 Phases 4+5 — Implementation Instructions (Coder)

**Contract:** `docs/specs/SPEC-UI-RESPONSIVENESS-2.md` §2.4 (Phase 4) + §2.5 (Phase 5).
The parent spec's line numbers PREDATE the Phase 1–3 rewrite of feed_handler.py —
the verified live line numbers below supersede the spec's. Read both.

**Work style:** steelFramedCodeWriter. Red-first tests per part. Two commits:
commit 1 = Phase 4, commit 2 = Phase 5. No resets (no-reset discipline).
`git add -A` is BANNED. Flag-don't-fix anything out of scope.

---

## PHASE 4 — Part A: in-place card update

**Target:** `ui/handlers/feed_handler.py` `update_card` (:989) + `ui/views/feed_card.py`
`build_feed_card` (:368). NOTE: build_feed_card moved OUT of feed_handler since the
spec was written — it now lives in `ui/views/feed_card.py`.

- Add `update_card_in_place(card_id, card_data)`: mutate the existing card widget's
  status badge / body label by reference instead of rebuilding the whole card.
- Widget must expose child refs. Precedent exists: `_finalize_snapshot` already
  checks `widget._context_panel`. Add `widget._status_label` / `widget._body_label`
  (or whatever the real child names are — read feed_card.py first) in
  `build_feed_card`, same pattern.
- `update_card` uses the in-place path when the widget exposes the needed children;
  falls back to the existing rebuild + `feed_tab.replace_card` (:260) otherwise.
  Both paths MUST still run the F1-amendment enqueue block (accepted-only-when-decided)
  and the idle_add widget swap fallback logic unchanged.
- RED FIRST: test that an in-place update keeps the SAME widget instance and updates
  labels; test the fallback path when children are missing.

## PHASE 4 — Part B: pre-loop prep off the main thread

**Target:** `ui/handlers/agent_runtime_handler.py` `send_to_special_agent` (:760).
Currently: `rt.load_conversation(session_key)` (:809, disk I/O + deserialize),
`rt._rebuild_conversation_context` (:816, project-switch path), and conversation
state syncing (api_key, model, MCP list, SI enforcement, step-count reset) all run
on the GTK main thread before the loop thread starts. (~300–500ms per send.)

- Extract the prep block into a `_prepare_turn()` closure; run it on the background
  thread. Main thread keeps only: show "thinking" indicator + start the thread.
- RISK (spec flags MEDIUM-HIGH): those mutations rely on implicit main-thread
  serialization. Under a lock, verify no concurrent reader (cancel, /clear, tab
  switch) can observe a half-prepared conversation. Reuse the RACE-FIX v4
  turn-token discipline for dispatched callbacks.
- Any UI-only work found in the prep block stays on the main thread (or dispatches
  via GLib.idle_add).
- RED FIRST: test that send_to_special_agent returns before load_conversation
  completes (e.g. slow fake rt) and that a cancel during prep doesn't crash.

## PHASE 4 — Part C: activity bubbles behind the 250ms ticker

**Targets:** `agent_runtime_handler.py` bubble emit sites :1266, :1381, :1412
(verified exact); `activity_handler.py` `_status_tick` 250ms ticker (:641, verified).

- Batch per-turn ActivityBubbles behind the existing 250ms cadence instead of one
  dispatch per tool start/result event.
- MUST NOT drop or reorder bubbles (per-session ordering preserved). Coalesce only.
- Watch the AC3 machinery: `_agent_name_for_event` lazy cache + skip-cache
  invalidation. Don't break either.
- RED FIRST: test that N rapid tool events produce ≤ ceil(N/interval) dispatches
  with correct final state and preserved order.

---

## PHASE 5 — git off the main thread

**Targets (live lines):** `feed_handler.py` — `on_filesystem_event` :1983,
`_finalize_snapshot` :2013, `_maybe_create_snapshot` :2047,
`_extract_messages_from_chat_box` :2097, writer thread :1102 (`crabcakes-feed-writer`),
`shutdown_persist_writer` :1366. `conversation_store.py` — `snapshot_from_messages` :33,
`snapshot_from_git_diff` :80. `crabwatch_handler.py` — 200ms debounce :109 (verified).

- **Edit A:** `on_filesystem_event` :1983 — REMOVE the inline
  `snapshot_from_git_diff` call; create the card with `conversation_snapshot=None`;
  the deferred path fills it.
- **DOUBLE-BUILD CHECK (found in pre-delegation read):** `add_card` (:756) schedules
  `_finalize_snapshot` → `_maybe_create_snapshot`, whose system/crabwatch branch
  ALSO calls `snapshot_from_git_diff`. With Edit A's inline call removed this
  becomes the single build site — verify, and make `_maybe_create_snapshot`
  early-return if a snapshot is already present (defensive).
- **Edit B:** run the snapshot build on a worker thread (reuse the Phase-1 writer
  thread or a second small daemon), then dispatch ONLY the widget update back via
  `GLib.idle_add`. SPLIT: `_extract_messages_from_chat_box` (:2097) is GTK-bound —
  stays main thread; `snapshot_from_messages` / `snapshot_from_git_diff` are pure —
  worker thread. `MAX_SNAPSHOT_MESSAGES` bounds the pure part.
- **Edit C:** multiple fs events for the same path on the same tick → at most one
  snapshot build. Keep the 200ms debounce; add last-write-wins per
  (project_path, file_path).
- RED FIRST: test that on_filesystem_event doesn't call snapshot_from_git_diff
  synchronously; test single-build for two same-tick events; test widget update
  arrives via idle_add (main thread).

---

## GATES (both phases)

1. RED-first evidence captured before each green.
2. GTK suites: `PYTHONDONTWRITEBYTECODE=1 xvfb-run -a python3 -m pytest <suite> -q`.
   Run suites individually. Relevant: test_feed_handler, test_feed_card (or the
   card-view suite), test_agent_runtime_handler (per-class if OOM — TestEndStreaming*
   classes are known OOM, run those separately or skip with documented baseline),
   test_activity_bubbles, test_crabwatch_handler, test_chat_render_handler.
3. pyflakes: /tmp/pf-venv2/bin/pyflakes (recreate venv if /tmp wiped — see
   context.md 2026-09-07 note for the PYTHONPATH shim). Gate: 0 undefined names;
   no new unused imports.
4. Hermeticity: no test writes to real ~/.config/crabcakes or real .crabcakes/feed*
   (use tmp_path + patch _FEED_PATH-class seams like prior phases).
5. Full-suite failure-set comparison vs pre-work baseline via clean git worktree in
   /tmp (established hermetic pattern). Failures must be identical sets; feed/agent
   suites must show only intended deltas.
6. STOP after commit 2. Report: red evidence, suites green + counts, pyflakes gate,
   any flags (do not fix out-of-scope items). Debugger audits next (adversarialDebugger).
