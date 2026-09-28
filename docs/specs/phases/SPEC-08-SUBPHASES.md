# SPEC-08 Subphases — Transcript Store (SQLite + WAL)

| SP | Scope | Files | Status | Commit |
|----|-------|-------|--------|--------|
| SP1 | Store core: schema, `append_turn`, `tail`, `load_all`, `delete_session`, `close`, WAL/busy_timeout verification + unit tests (tmp dirs, red-first) | utils/transcript_store.py (new), tests/test_transcript_store.py (new) | pending | — |
| SP2 | Persistence wrapper: 6-function contract preserved, dual-write (D3), watermark + sessions table (D4) + repointed tests | agent/persistence.py, tests/test_agent_persistence.py | pending | — |
| SP3 | Migration (JSON→DB batched, .migrated suffix, banner card, off-UI-thread) + two-writer concurrency test (2×500 → 1,000/1,000 zero lost) + corrupt-DB fallback test | agent/persistence.py, ui/ (banner card site), tests | pending | — |
| SP4 | Close-out: full suite + ruff + pyright, 11-section post-mortem, push | post-mortem | pending (supervisor-owned) | — |

**Rulings in force (PM, 2026-09-27):** D1=(c) global `<config_dir>/transcript.db` (ARCHITECTURE.md amended); D2 `delete_session()`; D3 dual-write one release; D4 sessions metadata table. Pre-flight analysis: `docs/specs/phases/SPEC-08-PREFLIGHT-DECISIONS.md`.

**Loop rules:** §3.1a audit every code-bearing turn pre-commit; steelFramedCodeWriter.md to Coder every build round; adversarialDebugger.md to Debugger every audit round; brief-anchors-verified (read target before writing Edit-N).
