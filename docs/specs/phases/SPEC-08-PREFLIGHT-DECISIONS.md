# SPEC-08 Pre-Flight Verification + Decision Package (2026-09-27, HEAD 3515a17e)

Supervisor pre-flight per implementationLoop §3.1a (pre-flight = code-bearing turn:
spec-vs-code drift audit BEFORE phase instructions are written).

## A. Verified facts

1. **persistence.py's real API is 6 functions, not 2** — spec §2 sketches only the
   save/load pair. Also live: `conversations_dir()`, `resolve_api_key_for_conversation()`
   (HIGH-3 secret hygiene), `migrate_conversation_files()` (HIGH-3 one-time strip),
   `resolve_session_workspace()` (LOW-2 path-escape guard). Any wrapper rewrite must
   preserve ALL six + their security contracts.
2. **The JSON "messages" shape is the real serialization contract** — 6 fields per
   message: role, content, tool_calls (call_id/tool_name/arguments), tool_call_id,
   tokens_used, timestamp. The DB schema's `tool_calls` column stores this exact shape
   as JSON — must round-trip identically or `load_conversation_from_disk` callers break.
6. **Config dir is `get_config_dir()`** = `$XDG_CONFIG_HOME/crabcakes` or
   `~/.config/crabcakes` (utils/config.py:14-21). Tests isolate via monkeypatching
   `utils.config.get_config_dir` (test_agent_persistence.py pattern). SPEC-11 renames
   this dir wholesale — the store must read it through `get_config_dir()`, never
   hardcode.
3. **Save sites (4)**: runtime `_auto_save` (:775), `stop()` (:775 block), explicit
   `save_conversation` (:2404), ARH:761. Load: runtime :2409 + startup paths. All
   keyed by session_key globally — NOT per-project.
4. **Session→project coupling is REAL but SOFT**: `conv.project_path` is written on
   save for audit but NOT trusted at load (always None; `_rebuild_conversation_context`
   re-applies from the live active project). A session's project can genuinely change
   mid-conversation (that's why the rebuild exists).
5. **HIGH-3 is load-bearing**: api_key NEVER serialized; re-resolved from
   providers.yaml on load. The store must not regress this (no api_key column, no
   secrets in rows).
6. **No runtime delete/clear API exists** — there is no `/clear`-style session wipe
   in runtime.py (the chat `/clear` is a gateway/agent-context concept, not a
   persistence wipe). Spec §7's "Session cleared (/clear)" edge case therefore has
   NO production trigger today; the seq-epoch design is dead code until group chat.
   BUT the JSON files ARE the thing users can delete by hand, and the store's
   append-only model changes that surface (see decision D2).
7. **Existing tests pin the JSON path**: test_agent_persistence.py (9 tests,
   monkeypatch pattern above) + test_agent_runtime.py:1308+ (XDG isolation) +
   status_report.py:161/1104 document/measure the JSON write order. Spec acceptance
   says "all existing tests pass unmodified" — impossible for tests that assert on
   .json file existence when saves go to SQLite. Needs an explicit reconciliation.

## B. Spec-vs-code drift found (fix the spec, Rule 4)

| # | Spec says | Code reality | Ruling |
|---|---|---|---|
| 1 | Store = per-project `<project>/.crabcakes/transcript.db` | Conversations are GLOBAL by session_key; a session's project can change mid-conversation (rebuild exists for exactly this) | **ESCALATE — decision D1** |
| 2 | "persistence.py becomes a thin wrapper … same public functions — callers unchanged" | 6 functions with security contracts (HIGH-3/LOW-2), not 2 | Spec §2 amended: full 6-function contract list |
| 3 | §7 "Session cleared (/clear)" — watermark-reset via sessions flag | No such API/trigger exists in runtime | Spec §7 amended: edge case re-marked post-MVP unless D2 rules otherwise |
| 4 | §6 "all existing tests pass unmodified" | Persistence tests assert JSON files; status_report measures JSON write order | Spec §6 amended per D3 |
|  5| §2 "diff-free append: wrapper computes delta vs watermark" | Runtime holds conv in memory under `self._lock`; save sites all inside that lock | Confirmed feasible; watermark approach OK |
| 6 | §4 File table lists only store + wrapper + tests | status_report.py documents the JSON write path in 2 places | Add to phase scope if D3 keeps JSON fallback |

## C. The phasing-blocking decision — D1: WHERE does the DB live?

**The conflict (authority chain applies: ARCHITECTURE.md > spec):**
ARCHITECTURE.md says: "SQLite + WAL **per project** (`.crabcakes/transcript.db`)" —
and adds "`agent/persistence.py` becomes thin wrapper".
The spec repeats per-project.
BUT the conversation model is global-by-session-key: every save/load today goes
through `conversations_dir()` (global), and a session may migrate across projects.
A per-project DB splits one session's history across N stores; load-all must then
know which project's store to read — but at load time (app start) there may be no
active project, or the wrong one. Worse: the runtime's own unit tests create
conversations with no project at all (tmp dirs).

**Options:**
- (a) **Global DB, one per install**: `<config_dir>/transcript.db` (next to
  conversations/). Single-writer is trivially enforceable (one module-level store).
  Sessions move across projects freely — history follows the session, matching
  today's semantics exactly. Migration reads `<config_dir>/conversations/*.json` —
  same dir, zero path ambiguity. SPEC-11 rename moves it with the config dir.
  Cost: deviates from ARCHITECTURE.md's "per project" phrase (one line to amend).
- (b) **Per-project DB (spec/architecture literal)**: requires session→project
  binding to be STRONG (it is explicitly soft today — rebuild exists because it
  drifts), a fallback store for project-less sessions, and multi-project load
  fan-out. Higher risk, more code, and contradicts the soft-binding design.
- (c) **Global now, shard later**: (a) + a documented evolution path (when group
  chat lands and sessions are truly project-bound, add a project column + optional
  sharding). Zero speculative complexity today.

**Supervisor lean: (c) — global `<config_dir>/transcript.db`, ARCHITECTURE.md
amended (one line), evolution documented.** It matches current semantics, kills the
last-writer-wins class with the least machinery, and keeps SPEC-11's config-dir
migration coherent. But ARCHITECTURE.md is the floor and this changes its letter —
per §5 Rule 1 this needs the captain's call, not mine alone.

## D. Secondary decisions (ride D1's ruling)

- **D2 — append-only vs delete surface**: with (a), sessions get a
  `delete_session(session_key)` (and the wrapper keeps working when the JSON file
  is hand-deleted). Recommend: store adds `delete_session`; JSON files remain
  authoritative until migration, then the DB is.
- **D3 — JSON fallback (spec §7 "DB corrupt → fall back to JSON")**: recommend
  KEEPING the fallback: wrapper writes JSON AND appends to DB for one release
  (dual-write), falls back on DB failure. Cost: the delta-watermark must live in
  the wrapper either way.
- **D4 — sessions metadata table**: spec §2's sessions table (model, totals) —
  recommend YES, per-session watermark + epoch lives there (D2's reset target).

## E. Recommended phasing (pending D1)

- SP1: store core (schema, append_turn, tail, load_all, close, WAL/busy_timeout
  verification) + unit tests on tmp dirs (red-first).
- SP2: persistence wrapper (6-function contract preserved; dual-write per D3;
  watermark/sessions table) + repointed/updated tests.
- SP3: migration (JSON→DB, .migrated suffix, banner card) + two-writer concurrency
  test (2×500, zero lost) + corrupt-DB fallback test.
- SP4 (supervisor): full suite, ruff, pyright, post-mortem, push.
