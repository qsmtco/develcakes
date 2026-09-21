# SPEC-08: Transcript Store (SQLite + WAL)

**Date:** 2026-09-20
**Author:** Supervisor (develcakes v2)
**Status:** Draft — for implementation
**Implements:** .crabcakes/architecture.md §Modules/Transcript store
**Depends on:** SPEC-07 (lands on the post-R4 surface stack)
**Target branch:** main

> Architecture compliance: append-safe under concurrent writers; single-writer
> discipline behind the module; kills the last-writer-wins class.

---

## 1. Overview

**Problem.** `agent/persistence.py` saves conversations as whole-file JSON rewrites
(`save_conversation_to_disk` → `json.dump` over the file). Two concurrent writers =
last-writer-wins = silently dropped turns. Group chat (post-MVP) makes this a daily
event; even today, Coder+Debugger running simultaneously race on their own files'
read-modify-write cycles via auto-save.

**Solution.** `utils/transcript_store.py` — SQLite per project
(`<project>/.crabcakes/transcript.db`), WAL mode, append-only turn rows, single-writer
discipline behind the module interface. `agent/persistence.py` becomes a thin wrapper
delegating to the store (same public functions — callers unchanged).

**Scope**

| In | Out |
|---|---|
| utils/transcript_store.py | Group-chat data model (post-MVP) |
| persistence.py → store delegation | Feed store changes |
| One-time JSON migration | JEV anything |
| Two-writer concurrency test | |

## 2. Changes by File

### utils/transcript_store.py (NEW)

```python
"""SQLite+WAL transcript store. Single-writer discipline behind this module."""

import sqlite3, threading, json, os

_SCHEMA = """
CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key TEXT NOT NULL,
    seq INTEGER NOT NULL,             -- per-session monotonic
    role TEXT NOT NULL,               -- user|assistant|tool|system
    content TEXT NOT NULL DEFAULT '',
    tool_calls TEXT,                  -- JSON array or NULL
    tool_call_id TEXT,
    tokens_used INTEGER DEFAULT 0,
    timestamp TEXT NOT NULL,
    UNIQUE(session_key, seq)
);
CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_key, seq);
"""

class TranscriptStore:
    def __init__(self, project_path: str):
        self._db_path = os.path.join(project_path, ".crabcakes", "transcript.db")
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        self._lock = threading.Lock()          # single-writer discipline
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)

    def append_turn(self, session_key: str, role: str, content: str,
                    tool_calls: list | None = None, tool_call_id: str | None = None,
                    tokens_used: int = 0) -> int:
        """Append one turn. Returns seq. Thread-safe via module lock."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT COALESCE(MAX(seq), -1) + 1 FROM turns WHERE session_key = ?",
                (session_key,))
            seq = cur.fetchone()[0]
            self._conn.execute(
                "INSERT INTO turns (session_key, seq, role, content, tool_calls,"
                " tool_call_id, tokens_used, timestamp)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                (session_key, seq, role, content,
                 json.dumps(tool_calls) if tool_calls else None,
                 tool_call_id, tokens_used))
            self._conn.commit()
            return seq

    def tail(self, session_key: str, n: int = 200) -> list[dict]:
        """Most-recent n turns (ascending). For render hydration."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM (SELECT * FROM turns WHERE session_key = ?"
                " ORDER BY seq DESC LIMIT ?) ORDER BY seq ASC",
                (session_key, n)).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def load_all(self, session_key: str) -> list[dict]:
        """Full history for context rebuild."""
        ...

    def migrate_from_json(self, conversations_dir: str) -> int:
        """One-time: read legacy <session_key>.json files, append rows, return count.
        Non-destructive: JSON files are NOT deleted (rename to .migrated suffix)."""
        ...
```

(Builder finalizes row→dict shape and adds `close()`; WAL verified via PRAGMA; the
UNIQUE(session_key, seq) index enforces append discipline at the DB layer too.)

### agent/persistence.py — thin wrapper

`save_conversation_to_disk(conv, session_key)` → diff-free append: since the runtime
holds the full conversation in memory, the wrapper computes the delta vs a per-store
appended-seq watermark and calls `append_turn` for new messages only; conversation
metadata (model, totals) goes to a small `sessions` table. `load_conversation_from_disk`
delegates to `load_all`. Public function signatures unchanged — verified callers
(runtime `_auto_save`, `load_conversation`, handler paths) compile untouched.

### Migration

On first store open: if legacy `<config_dir>/conversations/<sk>.json` exists and the
sessions table lacks the key → `migrate_from_json` → rename file to `<sk>.json.migrated`.
Banner feed card reports what moved (SPEC-02 card pattern).

## 3. Data Flow

Turn ends → `_auto_save` → persistence wrapper → delta vs watermark →
`append_turn(...)` rows → WAL commit. App start / tab open → `load_all` → Conversation
object → runtime. Render hydration → `tail(sk, 200)`.

## 4. File Change Summary

| File | Change | ~Lines | Risk |
|---|---|---|---|
| utils/transcript_store.py | new | +280 | med |
| agent/persistence.py | wrapper rewrite | ~200 rewrite | med-high (touching save/load of all sessions) |
| tests/test_transcript_store.py | new | ~250 | — |

## 5. Implementation Order

1. Store + unit tests (append/tail/load_all/migration on tmp dirs).
2. Persistence wrapper behind existing signatures; suite green.
3. Two-writer test: 2 threads × 500 turns interleaved → `load_all` returns exactly
   1,000 turns, zero lost, seqs contiguous.
4. Migration test: legacy JSON in → rows out → file renamed.
5. Full suite + ruff + pyright.

## 6. Acceptance Criteria

- [ ] Two-concurrent-writers test passes with **zero lost turns** (1,000/1,000)
- [ ] WAL mode on; `busy_timeout` honored under lock contention test
- [ ] JSON migration moves history; `.migrated` suffix; banner card
- [ ] persistence.py public API unchanged; all existing tests pass unmodified
- [ ] Full pytest green, ruff clean, pyright clean

## 7. Edge Cases

| Case | Behavior |
|---|---|
| DB file corrupt | Store refuses to open → runtime falls back to JSON path (wrapper keeps legacy code path for one release) + feed card |
| Very long content (>1 MB message) | Stored fine (SQLite TEXT); render truncates (SPEC-06 cap) |
| Session cleared (/clear) | Store keeps rows (audit trail); wrapper watermark resets via sessions table flag — cleared sessions start a new seq epoch |
| Project moved (path change) | DB is per-project relative — moves with the repo |
| Concurrent append + tail from render thread | Lock-serialized; tail sees committed state only |

## 8. ARCHITECTURE.md Updates

§Modules/Transcript store — mark implemented; note sessions-table metadata fields.
