# SPEC-08 SP4 PHASE A — Store-Mode Load + Banner Card + Flag Enable (delegated half)

**Spec:** `docs/specs/SPEC-08-TRANSCRIPT-STORE.md` AMENDED (§2: "load_conversation_from_disk
delegates to load_all" — the contract my SP2/SP3 briefs under-carried; this closes it).
**Builder rule:** `prompts/steelFramedCodeWriter.md` — invoke it, apply every rule.
**Depends on:** SP3 committed (43f59845).
**Touches:** `agent/persistence.py` (load), `agent/runtime.py` (setter + stale comment),
`ui/handlers/agent_runtime_handler.py` (banner card), `main.py` (flag), tests.

---

## Edit 1 — store-mode load (the load-gap ruling)

`load_conversation_from_disk(session_key)`:

- JSON present → existing JSON path unchanged (D3's one-release fallback).
- JSON absent → **store fallback**: `_get_store().load_all(sk)`; if rows exist,
  rebuild the `Conversation` from rows using the EXACT JSON deserialization shape
  (`Message(role=MessageRole(row["role"]), content=…, tool_calls=[ToolCall(call_id,
  tool_name, arguments)], tool_call_id, tokens_used)`; timestamps from
  `datetime.fromisoformat(row["timestamp"])` where parseable, else `datetime.now()`);
  metadata dict from the sessions table (`agent_name`, `model`, `provider`) with
  JSON-shape keys. api_key re-resolution UNCHANGED (HIGH-3 — the metadata dict
  feeds `resolve_api_key_for_conversation` the same way the JSON path's does).
- Rows absent AND JSON absent → None (today's behavior).
- Store failure (corrupt DB etc.) → log + return None (never raise; the JSON
  path's absence is not an error condition).
- **The dual-anchor guard now passes naturally:** store-hydrated sessions have
  rows == the message list by construction; `wm == len-1`; divergence only where
  compaction genuinely diverged.

**Tests** (`tests/test_agent_persistence.py` extend):
1. `test_store_mode_load_hydrates_when_json_absent` — save 3 via wrapper →
   rename JSON away (simulate migration) → load returns conv with exactly 3
   messages (roles/contents/tool_calls round-trip), metadata carries model/
   provider/agent_name, api_key resolved from providers (or None) — NEVER from rows.
2. `test_store_mode_load_matches_json_shape` — load-from-JSON vs load-from-store
   for the same session: identical message tuples (role, content, tool_calls,
   tool_call_id, tokens_used); the two paths are shape-equivalent.
3. `test_store_mode_guard_passes_after_hydration` — hydrate → append 2 → save →
   rows 0..4 (no divergence flag, wm 4) — the trap the ruling closes.
4. `test_store_mode_corrupt_store_returns_none` — poisoned store + no JSON →
   None (no raise), warning logged.

## Edit 2 — migration banner card (Ruling 2: handler-owned)

`agent/runtime.py`: add `set_on_store_migration(self, cb)` storing
`self._on_store_migration_callback`; `_start_store_migration`'s guarded dispatch
prefers it over `_log_store_migration_banner` when set. Fix the STALE docstring
(`guards on self._running` → `_stopped`; also the BUG#6 paragraph predates the
round-2 rename — one reword).

`ui/handlers/agent_runtime_handler.py` (~15 lines, at the existing runtime
construction site): register a receiver that builds a `FeedCardData` —
title: "Transcript migration complete" / "Transcript migration failed" (aborted)
/ "Transcript migration: N sessions need retry" (errors non-empty, migrated 0);
body: migrated/skipped/turns/seconds/kept-on-JSON/errors-count from the stats
dict; fires through the handler's existing feed-emission seam (the SPEC-02 card
pattern already used there).

**Tests:** `tests/test_migration.py` extend —
5. `test_banner_card_built_from_stats` — three shapes (success / aborted /
   all-errors) → card title/body fields asserted at the ARH receiver.
6. `test_progress_consumed` — `set_on_store_migration`-adjacent: wire
   `on_progress` through the runtime setter (add the param alongside on_complete)
   → heartbeat reaches the receiver (10/20/total cadence per the SP3 pin).

## Edit 3 — enable the flag (the one line)

`main.py`: `os.environ.setdefault("CRABCAKES_MIGRATE_STORE", "1")` before app
construction; UPDATE the pointer comment (no longer "do not set" — now "SP4
store-mode load makes rename safe; flag defaults on, override with =0 to skip").
Confirm the conftest autouse fixture still isolates tests from the now-default-on
sweep (the `_isolated_config_dir` fixture pins get_config_dir — verify the flag
path is inert under it; if any test breaks, report, don't force).

## Verification (paste full output — pyright MANDATORY, xvfb full suite)

```
env -u DISPLAY .venv/bin/python -m pytest tests/test_agent_persistence.py tests/test_migration.py tests/test_transcript_store.py -v
.venv/bin/python -m pyright agent/persistence.py agent/runtime.py ui/handlers/agent_runtime_handler.py main.py tests/test_migration.py tests/test_agent_persistence.py
python -m ruff check agent/persistence.py agent/runtime.py ui/handlers/agent_runtime_handler.py main.py tests/test_migration.py tests/test_agent_persistence.py
xvfb-run -a .venv/bin/python -m pytest tests/ -q --ignore=tests/test_enforcement.py --ignore=tests/test_mcp_config.py -p no:cacheprovider 2>&1 | tail -3
```

## COMPLETENESS (mandatory)

- [ ] Edit 1: store-mode load (JSON-first, store fallback, exact shape) + 4 tests
- [ ] Edit 2: set_on_store_migration + ARH FeedCardData receiver + stale-docstring fix + 2 tests
- [ ] Edit 3: flag on + comment + test-isolation confirmation
- [ ] Full battery green incl. pyright + full suite
- Related issues found, not fixed: <list or none>

Deviations carry a one-sentence rationale each.
