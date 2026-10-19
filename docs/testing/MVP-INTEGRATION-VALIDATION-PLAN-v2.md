# MVP Integration Validation Plan v2 — reconciled targets, priority-ordered

**Date:** 2026-10-19
**Author:** Supervisor
**Status:** Draft — targets only; execution awaits PM decision
**Supersedes:** `docs/testing/MVP-INTEGRATION-VALIDATION-PLAN.md` (2026-10-04) — its eight
*questions* survive; half its *machinery references* do not. That plan was written
at HEAD 469c7992 against a codebase that has since replaced its persistence layer
(SPEC-35), rewritten its feed as a WebKit document (SPEC-FEED-HTML-SURFACE), gained
compiled packets + wire normalization (SPEC-24/SPEC-40/SPEC-41), and retired the
GTK feed card list entirely.

---

## Why v2

Every subsystem passed its own audit in isolation. No test has ever driven the
subsystems *together*. The original plan named eight seams; a reconciliation
pass against the current tree shows:

- **6 of 8 seams survive with updated machinery** (different modules, same question)
- **1 seam's premise inverted** (I6: the feed is a document now, not widgets)
- **1 seam's subject is gone** (I4's `transcript.db` — persistence is one JSON
  file per session since SPEC-35)
- **4 new seams exist that the plan could not know about** — compiled packets,
  wire normalization, hot-window replay, feed document eviction — and the one
  production bug class found in the wild since (MiniMax 2013, found by
  Debugger's WU-10 audit) came from exactly such a seam.

## Reconciliation of the original eight

| Old | Question survives? | Machinery then | Machinery now | Disposition |
|---|---|---|---|---|
| I1 writer turn end-to-end | yes | transcript.db, worktree, lease, checkpoint, queue | same, minus transcript.db → `agent/persistence.py` per-session JSON | **RETARGET → N1** |
| I2 stop-all sweeps in flight | yes | turn cancel, process kill, checkpoint abort, approval deny | same modules (`stop_all_agents`, ARH cancel paths) | **KEEP → N3** |
| I3 provider error → no empty turn | yes | error card + transcript | same, + failover chain + failure classifier | **KEEP → N4** |
| I4 config-dir divergence | yes, but the subject changed | transcript.db migration | `agent/persistence.py` files land in new dir; old dir copied | **REWRITE → N6** |
| I5 env fallback chain | yes | `DEVELCAKES_`/`CRABCAKES_` latch | same (`utils/config.py:45-57`) | **KEEP (small) → N7** |
| I6 feed bounded memory | premise inverted | GTK widget window | document + `_backlog` + `feed.json` retention (`_evict_surplus_html_rows`) | **REWRITE → N5** |
| I7 sanitize at every call site | yes — more sites now | `render_message` sites | `render_document`/`live_fence`/error+system cards, sanitizer always-on | **KEEP, expanded → N8** |
| I8 app identity + launcher | partially | banner-on-first-run | app id + desktop file exist; no banner machinery found | **REDUCED → folded into N8** |

## New seams the plan could not know (added by Oct 5 → Oct 19 work)

| # | Seam | Why it is risky |
|---|---|---|
| N2 | compiled packet ↔ `normalize_tool_history` ↔ provider strictness | the MiniMax 2013 class: a wire shape we emit can be rejected by the strictest provider; already bit once in the wild (WU-10 audit BUG#2: `wire_from` split an assistant from its own results) |
| N9 | hot-window replay across enforced writes | SPEC-40's replay rides the compiled user message; a wrong coordinate or a stale body silently misleads the model |
| N10 | feed document ↔ eviction ↔ persistence ↔ Load More | already produced BUG#5 (reversed page) — the ordering/backlog invariants are load-bearing |

---

## Ordered targets (most important first)

Ordering rationale: (1) a seam that can hard-fail a provider turn outranks a
seam that only misleads the model; (2) data loss outranks cosmetics; (3) each
target is small enough for one dispatch under the 16-25 call discipline.

### N1 — Writer turn end-to-end (was I1) — **the crown jewel**
One test drives: lease → worktree per turn → enforced write in the worktree →
checkpoint serialization → review queue attribution → accept → per-session
persistence file updated. Assert every stage names the SAME turn.
**Modules:** `agent/persistence.py`, `utils/worktree_manager.py`,
`models/work_unit.py` (lease), ARH SPEC-10 paths, `ui/handlers/review_handler.py`.
**Risk covered:** the chain was never tested as a chain.

### N2 — Compiled wire ↔ normalization ↔ provider (NEW)
The compiled wire (packet + `wire_from`/`after` + hot windows) passes through
`normalize_tool_history` and must satisfy MiniMax/Anthropic strict adjacency.
Tests: every packet shape we emit (hot windows, line-mode, wire_from splits,
parallel calls, interleaved nudges) serializes to a wire with **every declared
tool_call id carrying a tool row**.
**Modules:** `agent/runtime.py` (`_compiled_messages`, `_maybe_compile`),
`agent/llm/convert.py`, `models/conversation.py`.
**Risk covered:** the exact class Debugger proved broken once (WU-10 BUG#2) —
and fixed once. This target keeps it fixed.

### N3 — Stop-all sweeps everything (was I2)
`stop_all_agents` while a turn is mid-flight: cancel + kill + checkpoint
abort + approval deny, asserted TOGETHER.
**Modules:** ARH `stop_all_agents`, runtime cancel paths, review handler deny.

### N4 — Provider error leaves no ghost (was I3)
Provider dies mid-turn: error card renders, per-session persistence records
the turn's real state, and no empty assistant message lands in history.
**Modules:** ARH error paths, `agent/persistence.py`, feed error cards.

### N5 — Feed document retention under reload (rewritten I6)
2,000-card project: live document window holds the cap, evicted cards stay in
`_cards` + `_backlog` + `feed.json`, reload hydrates, Load More restores —
and the document ORDER stays oldest-first throughout (the BUG#5 inversion).
**Modules:** `ui/feed/load.py`, `ui/views/feed_surface.py`, `utils/feed_store.py`.
**Risk covered:** bounded memory + no lost cards.

### N6 — Config-dir migration moves the conversation files (rewritten I4)
Point the config dir at a new location: per-session JSONs land in the NEW dir;
the OLD dir's files are copied; nothing writes to the old location after.
**Modules:** `utils/config.py`, `agent/persistence.py`.
**Risk covered:** data stranded in a stale directory.

### N7 — Env fallback chain (was I5, small)
`DEVELCAKES_X` wins over `CRABCAKES_X`; an explicit `DEVELCAKES_X=""` does not
resurrect the old var; the latch resolves identically from every import site.
**Modules:** `utils/config.py` (`get_env`).
**Risk covered:** the one-release fallback misresolving.

### N8 — Sanitize at every call site + app identity (was I7 + I8, expanded)
The sanitizer is in the path for: normal messages, error cards, system cards,
`live_fence`, `render_document`. Plus: app id `com.develcakes.app`, desktop
file Exec/Icon/StartupWMClass all `develcakes`.
**Modules:** `render/html.py`, `ui/handlers/chat_render_handler.py`, `main.py`,
`data/com.develcakes.app.desktop`.
**Risk covered:** injection through an odd corner; identity regressions.

---

## Execution shape (when PM says go)

- One target per dispatch, the established loop: Coder writes the seam tests →
  Supervisor verifies + gates (background protocol) → Debugger adversarially
  probes for what the tests miss.
- Phase A (this reconciliation) is done the moment the PM accepts the order.
- Targets N1/N2 first regardless of final order — they are the only two that
  can hard-fail a turn or strand a decision.
- The acceptance matrix report (`docs/testing/MVP-VALIDATION-REPORT.md`) is
  written LAST, as the aggregate of the per-target evidence.

## Deliverables (unchanged in kind)

1. `tests/test_mvp_integration.py` — the cross-seam tests, accumulated target by target.
2. `docs/testing/MVP-VALIDATION-REPORT.md` — matrix + results + gaps.
3. Fix-round post-mortems if any target finds a production defect.
