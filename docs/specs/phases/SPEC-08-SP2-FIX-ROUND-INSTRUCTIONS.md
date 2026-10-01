# SPEC-08 SP2 FIX ROUND — audit BUG #1–#6 (Debugger, 2026-09-27) + watermark contract ruling

**Builder rule:** `prompts/steelFramedCodeWriter.md` — invoke it, apply every rule.
**Supervisor ruling (binding):** the watermark is "appended through," never
"acknowledged through": `wm == max(seq of rows)` or `-1`. It is a DERIVED fact
about rows. No API may set it independent of the rows that back it.

## BUG #2/#3 (HIGH) — delete `sync_watermark`; the delta loop is the migration

1. DELETE `sync_watermark` from `utils/transcript_store.py` (method + any tests
   referencing it). No replacement write surface — per the ruling, none is allowed.
2. DELETE the sync-on-load block from `load_conversation_from_disk` (keep NO store
   interaction on load — load is pure JSON this release).
3. REPLACE `test_load_syncs_watermark_after_json_only_restart` with
   `test_json_only_restart_backfills_on_next_save`: delete the store DB (simulate
   restart-with-lost-store), `load_conversation_from_disk` (3 msgs), save again
   UNCHANGED → store shows ALL 3 rows (wm=2), NOT 0 — the delta loop backfills
   because wm=-1. Assert `session_watermark == 2` and `len(load_all) == 3`.
4. NEW invariant pin `test_watermark_equals_max_row_seq` (store test file): after
   every wrapper-style operation sequence (append ×k, delete, append again),
   `session_watermark(sk)` equals `max(seq)` from a fresh query, or -1 when no rows.
   Must be able to fail: if any future API lets wm drift ahead of rows, this dies.

## BUG #4 (MEDIUM) — atomic delta: `append_delta` in the store

Add to `utils/transcript_store.py`:

```python
def append_delta(self, session_key: str, base_idx: int, rows: list[dict]) -> int:
    """Atomically append an index-aligned run of turns past the watermark.

    One lock acquisition + one BEGIN IMMEDIATE: reads wm INSIDE the tx, starts
    at max(wm + 1, base_idx), writes rows[start - base_idx + i] with seq =
    start + i (explicit-seq invariant), upserts watermark to the last written
    seq, commits. Two racing savers serialize here; the second re-derives from
    the first's COMMITTED wm — the union of both deltas survives.

    rows: list of dicts with keys role, content, tool_calls, tool_call_id,
    tokens_used (the persistence shape; NO api_key — HIGH-3).
    Returns the last seq written, or -1 if nothing to append.
    """
```

Rollback-on-fail per the SP1 pattern. On IntegrityError: rollback + raise (the
wrapper's fallback catches it; the next save re-derives from committed state).

Repoint `_append_conversation_delta` (persistence.py) to ONE `append_delta` call:
pass `base_idx=0` and the full message list is allowed but wasteful — pre-trim
with a best-effort `session_watermark()` read OUTSIDE the lock (optimization
only; correctness is owned by the in-tx re-read), then `append_delta(sk, wm+1,
shaped_messages)`.

NEW test `test_concurrent_saves_union_preserved` (persistence tests): two
threads, same session key, conv A with 5 messages and conv B with 8 (shared
first 5), Barrier start, both `save_conversation_to_disk`. Assert: no escaped
exception, store has rows 0..7 (union), wm == 7, contents match the 8-message
conv for 0..4 (either writer's shared prefix) and B's tail for 5..7.

## BUG #1 (HIGH) — the HIGH-3 proof must sweep WAL sidecars

Fix `test_no_api_key_in_store`:
1. `store` (or a fresh instance on the same path) runs
   `PRAGMA wal_checkpoint(TRUNCATE)` before reading.
2. Sweep ALL THREE files: `_db_path`, `+ "-wal"`, `+ "-shm"` (read bytes; skip
   nonexistent sidecars).
3. STRENGTHEN the probe per the audit's finding: plant a secret-shaped value
   INSIDE tool_calls arguments (`{"env": {"OPENAI_API_KEY": "sk-LEAKED-IN-ARGS"}}`)
   in a message, save, and assert the sweep DOES find it when run against a
   deliberately-leaky control — i.e. add a second assertion path proving the
   sweep itself has teeth (mutation-style: the sweep must be able to fail).
   The production assertion stays: no `sk-`-shaped string in any of the three
   files after a normal save.

## BUG #6 (MEDIUM) — 0600 perms on db/-wal/-shm + 0700 config dir

In `TranscriptStore.__init__`, after connect + WAL retry:
- `os.chmod(db_path, 0o600)` wrapped in try/except OSError (non-POSIX).
- After the first operation that creates sidecars (or immediately post-init if
  they exist): chmod `-wal` and `-shm` to 0o600 when present. Since sidecars
  appear on first write, ALSO chmod them lazily: do the chmod attempt in
  `append_turn`'s tx? NO — simpler and sufficient: chmod db + any EXISTING
  sidecars at init, and add a one-time post-first-append chmod inside
  `append_delta`/`append_turn` guarded by a `self._perms_set` flag. Document
  why (WAL sidecars inherit umask; HIGH-3 parity with JSON's 0600).
- Config-dir 0700: `__init__` after makedirs — `os.chmod(parent, 0o700)` best-effort.
- NEW test `test_db_files_0600`: after init + one append, `stat.S_IMODE` of db,
  -wal, -shm each == 0o600 (skip -shm/-wal gracefully if absent on the platform,
  but assert db always).

## BUG #5 (MEDIUM) — no orphan connection on failed init

Wrap the post-connect init (`_wal_retry` → pragmas → `executescript`) in
try/except: on failure, `self._conn.close()` then re-raise. NEW test
`test_failed_init_closes_connection`: corrupt/garbage db file bytes → construct
TranscriptStore raises; assert fd count stable across 50 construction attempts
(with `gc.disable()` during the loop, `gc.enable()` after — try/finally).

## Debugger's suggestion (fold in) — one singleton-path test

`test_real_singleton_path_used_when_override_cleared` (persistence tests): save
`_store_override = None` + `_store_singleton = None`, monkeypatch
`utils.config.get_config_dir` to tmp, call `save_conversation_to_disk` — the
REAL `_get_store()` singleton path runs, DB lands under the patched config dir.
Restore both to None in teardown (fixture) so no cross-test bleed.

## Verification (paste full output)

```
env -u DISPLAY .venv/bin/python -m pytest tests/test_transcript_store.py tests/test_agent_persistence.py -v
timeout 110 env -u DISPLAY .venv/bin/python -m pytest tests/test_projects.py tests/test_conversation.py -q
python -m ruff check agent/persistence.py utils/transcript_store.py tests/test_agent_persistence.py tests/test_transcript_store.py
```

Full suite (xvfb-run, MANDATORY per the process rule) — run it, paste the tail:
```
xvfb-run -a .venv/bin/python -m pytest tests/ -q --ignore=tests/test_enforcement.py --ignore=tests/test_mcp_config.py -p no:cacheprovider 2>&1 | tail -3
```

## COMPLETENESS (mandatory)

- [ ] BUG#2/#3: sync_watermark deleted (method + call + tests); backfill test replaces it; invariant pin added
- [ ] BUG#4: append_delta (atomic, in-tx wm re-read) + wrapper repointed + union test
- [ ] BUG#1: checkpoint + 3-file sweep + teeth-proof control path
- [ ] BUG#6: 0600 x3 + 0700 dir + perms test
- [ ] BUG#5: init-failure closes connection + fd-stability test
- [ ] Singleton-path test added
- [ ] Full battery green (xvfb full suite pasted); ruff clean/at-baseline
- Related issues found, not fixed: <list or none>

Deviations carry a one-sentence rationale each.
