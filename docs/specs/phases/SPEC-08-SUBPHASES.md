# SPEC-08 Subphases — Transcript Store (SQLite + WAL)

| SP | Scope | Files | Status | Commit |
|----|-------|-------|--------|--------|
| SP1 | Store core: schema, `append_turn`, `tail`, `load_all`, `delete_session`, `close`, WAL/busy_timeout verification + unit tests (tmp dirs, red-first) | utils/transcript_store.py (new), tests/test_transcript_store.py (new) | ✅ done (audit: BUG#1 HIGH + #4 + teeth fixed; 21/21 mutations; re-audit re-confirmed) | b30fed78 |
| SP2 | Persistence wrapper: 6-function contract preserved, dual-write (D3), watermark + sessions table (D4) + repointed tests. Fix rounds 1–3: atomic append_delta (wm re-read IN-TX), -1 sentinel on full-skip, wm=append-only-ledger ruling (sync deleted), BUG#5 WAL race retry, BUG#6 0600×3 perms, round-3 DUAL-ANCHOR guard (seq-wm boundary + seq-0, emptiness-first) killing middle-trims (real DefaultContextStrategy keep_first=2 shape) + full-clear IndexError | agent/persistence.py, utils/transcript_store.py, tests/test_agent_persistence.py, tests/test_transcript_store.py | ✅ built + battery green (4017 passed / 2 skipped; pyright 0) — awaiting Debugger re-audit | — |
| SP3 | Migration (JSON→DB batched, .migrated suffix, banner card, off-UI-thread) + two-writer concurrency test (2×500 → 1,000/1,000 zero lost) + corrupt-DB fallback test | agent/persistence.py, ui/ (banner card site), tests | pending | — |
| SP4 | Close-out: full suite + ruff + pyright, 11-section post-mortem, push | post-mortem | pending (supervisor-owned) | — |

**Rulings in force (PM, 2026-09-27):** D1=(c) global `<config_dir>/transcript.db` (ARCHITECTURE.md amended); D2 `delete_session()`; D3 dual-write one release; D4 sessions metadata table. Pre-flight analysis: `docs/specs/phases/SPEC-08-PREFLIGHT-DECISIONS.md`.

**Loop rules:** §3.1a audit every code-bearing turn pre-commit; steelFramedCodeWriter.md to Coder every build round; adversarialDebugger.md to Debugger every audit round; brief-anchors-verified (read target before writing Edit-N).
