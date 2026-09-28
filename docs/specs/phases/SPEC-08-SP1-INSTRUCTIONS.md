# SPEC-08 SP1 — Transcript Store Core (SQLite + WAL), 1 of 3

**Spec:** `docs/specs/SPEC-08-TRANSCRIPT-STORE.md` (READ THE §AMENDED HEADER — it
supersedes the original sketch where they differ; the sketch's `project_path` ctor is
DEAD, replaced by `db_path: str | None = None` defaulting to `<config_dir>/transcript.db`).
**Builder rule:** `prompts/steelFramedCodeWriter.md` — invoke it, apply every rule.
**This phase touches:** `utils/transcript_store.py` (NEW), `tests/test_transcript_store.py`
(NEW). Nothing else.

---

## What you are building

The append-safe store that kills last-writer-wins. Pure Python + sqlite3 stdlib —
NO GTK, NO network, NO agent/ imports (this module sits BELOW the agent layer; the
architecture forbids ui→agent imports and this is utils-layer).

Verified facts you can rely on (HEAD 3515a17e):
- `utils/config.py:14` — `get_config_dir()` respects `$XDG_CONFIG_HOME`, else
  `~/.config/crabcakes`. Import it INSIDE the default-branch (test isolation
  monkeypatches `utils.config.get_config_dir`).
- The JSON message shape (the serialization contract the DB must round-trip; from
  agent/persistence.py save): role (str: "system"|"user"|"assistant"|"tool"),
  content (str), tool_calls (list of {call_id, tool_name, arguments} — may be
  empty/None), tool_call_id (str|None), tokens_used (int), timestamp (ISO-8601 str).

## Edit 1 — `utils/transcript_store.py` (NEW)

Schema (spec §2, plus D2/D4):

```sql
CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key TEXT NOT NULL,
    seq INTEGER NOT NULL,             -- per-session monotonic
    epoch INTEGER NOT NULL DEFAULT 0, -- D4: bump = new epoch (post-MVP /clear)
    role TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    tool_calls TEXT,                  -- JSON array (persistence shape) or NULL
    tool_call_id TEXT,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    timestamp TEXT NOT NULL,
    UNIQUE(session_key, epoch, seq)
);
CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_key, epoch, seq);
CREATE TABLE IF NOT EXISTS sessions (
    session_key TEXT PRIMARY KEY,
    agent_name TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    provider TEXT,
    watermark INTEGER NOT NULL DEFAULT -1,  -- last appended seq (this epoch)
    epoch INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT
);
```

Class `TranscriptStore`:

- `__init__(self, db_path: str | None = None)` — default via `get_config_dir()`;
  `os.makedirs(dirname, exist_ok=True)`; `sqlite3.connect(check_same_thread=False)`;
  PRAGMA `journal_mode=WAL`; PRAGMA `busy_timeout=5000`; executescript schema. Module
  docstring: single-writer discipline lives HERE.
- `append_turn(session_key, role, content, tool_calls=None, tool_call_id=None,
  tokens_used=0) -> int` — under `threading.Lock`: SELECT MAX(seq)+1 within the
  session's CURRENT epoch, INSERT, upsert sessions row (watermark=seq,
  updated_at), commit, return seq. `tool_calls` serialized with the persistence
  shape (`json.dumps` when non-empty). Timestamp: SQLite
  `strftime('%Y-%m-%dT%H:%M:%fZ','now')` (matches spec sketch).
- `tail(session_key, n=200) -> list[dict]` — most-recent n of the CURRENT epoch,
  ascending. Row→dict shape: the 8 turn columns as a plain dict (keys exactly:
  session_key, seq, epoch, role, content, tool_calls (PARSED list or None),
  tool_call_id, tokens_used, timestamp).
- `load_all(session_key) -> list[dict]` — all epochs, ascending (epoch, then seq).
- `delete_session(session_key) -> int` (D2) — DELETE turns + sessions row for the
  key (ALL epochs); return rows deleted. Under lock, commit.
- `session_watermark(session_key) -> int` — watermark of current epoch, -1 if
  unknown session.
- `bump_epoch(session_key) -> int` (D4) — sessions row: epoch += 1, watermark = -1;
  return new epoch. (Post-MVP trigger; exercised by tests now.)
- `close()` — commit + close; idempotent (second call no-op).
- **HIGH-3 guard:** the store NEVER accepts or stores api_key — add a module-level
  assertion in tests (Edit 2, test 12) that no column name contains "key"/"api"
  and that appending a dict WITH an api_key field via tool_calls… no — simpler:
  the store's API surface simply has no such parameter; the test asserts
  `PRAGMA table_info` column names are exactly the 10 turn + 7 sessions columns.

## Edit 2 — `tests/test_transcript_store.py` (NEW; red-first)

All tests on `tmp_path` DBs (`TranscriptStore(db_path=str(tmp_path / "t.db"))`)
except the default-path test which monkeypatches `utils.config.get_config_dir`.
NO gi imports — the file must run bare (`env -u DISPLAY`).

Required coverage (name them exactly):

1. `test_append_and_load_all_roundtrip` — 3 turns across roles; load_all returns
   3 dicts, seqs 0,1,2 contiguous, shapes exact.
2. `test_tool_calls_roundtrip_persistence_shape` — tool_calls list of the
   persistence shape round-trips as PARSED list (not str).
3. `test_tail_returns_last_n_ascending` — 10 turns, tail(4) → seqs 6..9 ascending.
4. `test_sessions_isolated` — two keys interleaved; each load_all sees only its own.
5. `test_watermark_tracks_appends` — -1 before, 0/1/2 as appends land.
6. `test_delete_session_removes_all_rows` (D2) — append 3, delete → 3, load_all
   empty, watermark -1, second delete → 0.
7. `test_bump_epoch_starts_new_seq` (D4) — 2 turns (seq 0,1); bump → 1; next
   append → seq 0 in epoch 1; load_all returns BOTH epochs (2+1 rows), ordered.
8. `test_wal_mode_enabled` — `PRAGMA journal_mode` returns "wal".
9. `test_busy_timeout_set` — `PRAGMA busy_timeout` returns 5000.
10. `test_close_idempotent` — close(); close(); no raise.
11. `test_default_path_uses_config_dir` — monkeypatch get_config_dir → tmp;
    TranscriptStore() creates `<tmp>/transcript.db` (file exists after an append).
12. `test_schema_has_no_secret_columns` — PRAGMA table_info on both tables;
    assert exact column-name sets (the 10 + 7 above); assert no column name
    contains "api" or "key" substring (HIGH-3).
13. `test_concurrent_append_two_threads_zero_lost` (the acceptance-core, early) —
    2 threads × 250 turns each, distinct sessions; all 500 present, seqs
    contiguous per session. (SP3 scales to 2×500; this pins the property NOW.)

Red-first: write tests FIRST, run, paste the red (import error / AttributeError),
then implement, then paste green.

## Verification commands (paste full output)

```
env -u DISPLAY .venv/bin/python -m pytest tests/test_transcript_store.py -v
python -m ruff check utils/transcript_store.py tests/test_transcript_store.py
python -m pytest tests/test_agent_persistence.py -q    # must stay green (untouched)
```

## COMPLETENESS (mandatory — a response without it is returned unread)

- [ ] Edit 1: store module — schema + 8 public methods, docstring names the discipline
- [ ] Edit 2: 13 tests, red-first proof pasted, green pasted
- [ ] Bare-safe run green; ruff clean on both new files
- [ ] test_agent_persistence.py untouched + green
- Related issues found, not fixed: <list or none>

Deviations carry a one-sentence rationale each.
