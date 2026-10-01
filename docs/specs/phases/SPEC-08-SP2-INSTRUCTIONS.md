# SPEC-08 SP2 — Persistence Wrapper: Dual-Write + Watermark (2 of 3)

**Spec:** `docs/specs/SPEC-08-TRANSCRIPT-STORE.md` (AMENDED header — read first).
**Builder rule:** `prompts/steelFramedCodeWriter.md` — invoke it, apply every rule.
**Depends on:** SP1 landed (store + 17 tests; BEGIN IMMEDIATE write discipline).
**This phase touches:** `agent/persistence.py`, `utils/transcript_store.py` (ONE
addition: BUG#5 retry), `tests/test_agent_persistence.py` (extend), plus
`utils/status_report.py` (2 doc-line updates ONLY — no behavior).

---

## What you are doing

Turning persistence.py into the dual-write wrapper (D3): the JSON path stays
(authoritative fallback for one release + the file-existence assertions in existing
tests), and every save ALSO appends the delta to the TranscriptStore. Load prefers
nothing yet (JSON still loads; SP3 switches) — BUT load must now ALSO hydrate the
store's watermark so the delta logic is correct after a JSON-only restart.

## Edit 0 — SP1 registers folded in (store file)

1. **BUG#5:** bounded retry around `PRAGMA journal_mode=WAL` in `TranscriptStore.__init__`
   — 5 attempts × 10 ms (audit-measured 0/160 failures). Keep `busy_timeout` order as-is.
   Add a concurrent-first-open test (4 threads, barrier, zero failures) to
   tests/test_transcript_store.py.
2. **MF4:** add ONE failure-injection test covering `delete_session` AND `bump_epoch`
   rollback paths (same delegating-connection pattern as the append injection test —
   assert error propagates, no dangling tx, connection recovers).
3. Rescope `test_two_instances_interleave_distinct_sessions`'s docstring: it pins
   per-session seq isolation under contention, NOT the BUG#1 race (0/5 pre-fix
   detection — the same-session pin is the race pin).

## Edit 1 — persistence.py: store acquisition + watermark state

- Module-level lazy singleton: `_get_store() -> TranscriptStore` (created on first
  use via `TranscriptStore()` — the global default path; a module-level override
  `_store_override` for tests). NO store import at module top (keep the lazy
  import discipline — persistence is imported before config is patched in tests).
- `save_conversation_to_disk(conv, session_key)`: KEEP the entire existing JSON body
  (unchanged, byte-for-byte behavior — HIGH-3 chmod, field order, everything), then
  AFTER the JSON write succeeds, append the delta:
  `store = _get_store()` → `wm = store.session_watermark(sk)` → for each message
  index > wm: `store.append_turn(sk, role, content, tool_calls=persistence_shape,
  tool_call_id, tokens_used)`. The delta compare is **INDEX/watermark-based ONLY** —
  never value-based (audit note: `tool_calls=[]`→None conflation would diff-loop a
  value compare). If the store raises: log + feed-card-able warning, JSON already
  saved — save() still returns the JSON path (D3 fallback contract).
- `load_conversation_from_disk(sk)`: existing JSON body unchanged; after a successful
  load, sync the store watermark UP if JSON has MORE messages than the store
  (`max(wm, len(messages)-1)` — a JSON-only restart must not re-append history).
  Guard: only when the store is healthy (wrap in try/except; store failure never
  breaks load).
- The other 4 functions: NO changes (their contracts are orthogonal to the store).
- HIGH-3 audit guard: the store path NEVER touches api_key — `resolve_api_key_for_conversation`
  stays JSON-only. Add ONE test proving a store round-trip contains no api_key
  anywhere (query the sessions+turns tables for the string "sk-" — none).

## Edit 2 — watermark epoch correctness

`bump_epoch` is post-MVP; the wrapper never calls it this release. Document that in
`_get_store`'s docstring (one line) so SP3+ doesn't reinvent.

## Edit 3 — tests/test_agent_persistence.py (extend, don't rewrite)

Keep ALL 9 existing tests untouched (they must pass unmodified — spec acceptance).
Add (tmp_path store override + get_config_dir monkeypatch pattern):

1. `test_save_appends_delta_to_store` — save a 3-message conv; store shows 3 turns,
   watermark 2; save again with 1 more message → store shows 4 (not 7).
2. `test_save_twice_same_conv_no_duplicates` — save; save again unchanged → store
   still 3 turns (watermark gate holds; no re-append).
3. `test_store_failure_falls_back_to_json` — override store with a raising fake;
   save succeeds (JSON exists); no exception escapes.
4. `test_load_syncs_watermark_after_json_only_restart` — save 3 via JSON+store;
   NEW store instance (fresh override simulating restart with empty store — actually:
   delete the db file); load succeeds; save again → appends ONLY the delta (3 again,
   not 6). This pins the JSON-only-restart hydration.
5. `test_no_api_key_in_store` — the HIGH-3 proof (Edit 1's guard).

## Edit 4 — utils/status_report.py (doc-only, 2 lines)

:161 and :1104/:1131 area reference save's JSON write order/atomicity. Update the
wording to note the dual-write (JSON write unchanged; store append is delta-based
after). NO behavior change — if either line is load-bearing in a test, REPORT it,
don't force it.

## Verification (paste full output)

```
env -u DISPLAY .venv/bin/python -m pytest tests/test_transcript_store.py tests/test_agent_persistence.py -v
env -u DISPLAY .venv/bin/python -m pytest tests/ -q --ignore=tests/test_enforcement.py --ignore=tests/test_mcp_config.py -p no:cacheprovider 2>&1 | tail -3
python -m ruff check agent/persistence.py utils/transcript_store.py utils/status_report.py tests/test_agent_persistence.py tests/test_transcript_store.py
```

(xvfb-run for the full suite; the two targeted files are bare-safe.)

## COMPLETENESS (mandatory)

- [ ] Edit 0.1: BUG#5 retry + concurrent-first-open test
- [ ] Edit 0.2: MF4 injection test (delete_session + bump_epoch)
- [ ] Edit 0.3: distinct-sessions docstring rescope
- [ ] Edit 1: dual-write save + watermark-sync load + raising-store fallback
- [ ] Edit 1 rider: no-api-key-in-store test
- [ ] Edit 2: epoch note in _get_store docstring
- [ ] Edit 3: 5 new persistence tests; 9 existing untouched+green
- [ ] Edit 4: status_report doc lines updated (or reported if load-bearing)
- [ ] Full battery green; ruff clean
- Related issues found, not fixed: <list or none>

Deviations carry a one-sentence rationale each.
