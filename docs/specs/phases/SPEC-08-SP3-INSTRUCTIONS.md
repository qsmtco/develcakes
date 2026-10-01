# SPEC-08 SP3 — JSON→Store Migration + Concurrency Acceptance (3 of 3)

**Spec:** `docs/specs/SPEC-08-TRANSCRIPT-STORE.md` (AMENDED header — migration section:
batched commits, off-UI-thread, banner card).
**Builder rule:** `prompts/steelFramedCodeWriter.md` — invoke it, apply every rule.
**Depends on:** SP2 landed (dual-write wrapper + watermark machinery).
**This phase touches:** `agent/persistence.py` (migration fn), `ui/` (ONE banner-card
site or a persistence-owned callback), `tests/` (new migration + acceptance tests).
**Scale fact:** 3,491 files / 124 MB live today — batch, don't per-file-commit.

---

## Goal

One-time migration: every legacy `<config_dir>/conversations/<sk>.json` that the
store hasn't seen becomes rows; the file is renamed `<sk>.json.migrated`; a banner
feed card reports count + duration. Plus the spec's two acceptance tests at full
scale: 2×500 two-writer concurrency and the corrupt-DB fallback.

## Edit 1 — `migrate_from_json` in agent/persistence.py

```python
def migrate_conversations_to_store(on_progress=None) -> dict:
    """One-time JSON→store migration. Idempotent + resumable.

    For each <sk>.json in conversations_dir(): if the store holds at least as
    many turns for sk as the file has messages (COUNT-based check, NOT the
    watermark — the SP2 audit's BUG#3: load-sync could set wm ahead of rows;
    the ruling deleted that path, but COUNT is the belt-and-braces predicate)
    AND the session is NOT diverged-flagged (fix-round-2: compacted sessions
    are JSON-only forever; renaming them would cement a store missing turns),
    skip (dual-write already caught up); else append the missing tail
    (explicit-seq appends aligned to JSON indexes, same as _append_conversation_delta)
    then rename the file to <sk>.json.migrated. Diverged sessions: NOT renamed,
    counted in the banner as "kept on JSON (compacted)".
    Batches: one commit per N=50 sessions (store.append_turn commits per turn —
    acceptable; the batch boundary is the sessions-loop, not per-file fsync).
    Returns {"migrated": n_sessions, "turns": n_turns, "skipped": n, "seconds": t,
             "errors": [(session_key, repr(e)), ...]}.
    on_progress(cb(done, total)) if provided.
    Non-destructive: NO file is deleted; unreadable files are counted in errors
    and LEFT AS-IS (never renamed — a retry next launch can attempt them again).
    """
```

Rules:
- Reuses `_get_store()`; explicit-seq appends (SP2 invariant: store seq == JSON index).
- Migration failure for one session NEVER aborts the sweep (collect, continue).
- Renaming AFTER the appends succeed, per session, inside the loop.
- `on_progress` is called at most every 10 sessions (UI heartbeat, not spam).

## Edit 2 — launch wiring (off-UI-thread)

Migration runs ONCE per process on first store use — NOT blocking app start.
Wire it where the runtime already bootstraps persistence (runtime.py:493 calls
`migrate_conversation_files()` today — the HIGH-3 sweep; the new migration runs
RIGHT AFTER it, in the same place). Run it on a `threading.Thread(daemon=True)`
started from runtime init; the banner card fires from the callback ON THE MAIN
LOOP (GLib.idle_add — the daemon thread must not touch GTK directly).

Banner card (SPEC-02 pattern): title "Transcript migration complete", body with
migrated/skipped/turns/seconds/errors-count. Fire ONLY when migrated > 0 (a clean
install with zero legacy files must not produce a card). If errors: append
"N sessions failed and will retry next launch".

**Wiring is the riskiest edit** (runtime.py init order + threading + GTK). If the
banner-card plumbing into the feed from runtime init is more than ~15 lines of
new wiring, STOP and report — we'll take a supervisor ruling on placement rather
than force a bad seam. The migration itself must land regardless.

## Edit 3 — tests (new file tests/test_migration.py)

All on tmp_path config dirs (monkeypatch get_config_dir) + conftest store override:

1. `test_migrate_basic` — 3 legacy files (2, 5, 0 messages) → migrated=3, store
   load_all matches each file's messages exactly (roles/contents/seqs), files
   renamed .migrated, originals gone.
2. `test_migrate_idempotent_resumable` — run migration; run again → migrated=0,
   skipped=3 (or files already renamed → not found); store unchanged.
3. `test_migrate_partial_failure_continues` — one corrupt JSON (write garbage) →
   that file NOT renamed, counted in errors; the other 2 migrate fine.
4. `test_migrate_skips_caught_up_sessions` — a session already dual-written
   (save via wrapper, store rows == file count) → skipped, NOT re-appended, file
   still renamed (it IS fully migrated — rename marks done). MUST also assert
   `len(store.load_all(sk)) >= file_message_count` after migration (the
   BUG#3 teeth: a watermark-only skip that discards rows fails this).
5. `test_migrate_leaves_diverged_sessions_on_json` (fix-round-2) — a
   diverged-flagged session (compaction-simulated): NOT renamed, NOT appended,
   counted in the banner's kept-on-JSON list, zero new store rows.
5. `test_banner_fires_only_when_work_done` — 0 legacy files → no card callback;
   3 files → one card call with the stats dict.
6. `test_two_writers_500_each_zero_lost` (spec §5.3 acceptance, full scale) — two
   store instances, same session, 500+500 via Barrier threads; fresh-connection
   count == 1000, seqs contiguous, watermark 999. (Same session is the hard case;
   distinct-sessions variant already exists.)
7. `test_corrupt_db_falls_back_to_json` (spec §7 edge) — write a garbage
   transcript.db into the config dir; construct wrapper save (JSON write must
   succeed + log the store failure); load from JSON must succeed. The store
   failure path is the D3 fallback — already proven at unit level in SP2's
   raising-store test; THIS test pins it with a REAL corrupt file (sqlite
   "file is not a database" error class).

## Verification (paste full output)

```
env -u DISPLAY .venv/bin/python -m pytest tests/test_migration.py tests/test_agent_persistence.py tests/test_transcript_store.py -v
xvfb-run -a .venv/bin/python -m pytest tests/ -q --ignore=tests/test_enforcement.py --ignore=tests/test_mcp_config.py -p no:cacheprovider 2>&1 | tail -3
python -m ruff check agent/persistence.py agent/runtime.py tests/test_migration.py
```

## COMPLETENESS (mandatory)

- [ ] Edit 1: migrate_conversations_to_store (idempotent, resumable, error-collecting)
- [ ] Edit 2: runtime wiring off-UI-thread + banner card (or STOP-and-report per the rule)
- [ ] Edit 3: 7 tests incl. 2×500 acceptance + corrupt-DB fallback
- [ ] Full battery green; ruff clean
- Related issues found, not fixed: <list or none>

Deviations carry a one-sentence rationale each.
