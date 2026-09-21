# docs/archive — superseded v1 documentation

Archived 2026-09-20 during develcakes v2 spec-planning (before the 11 new v2 spec files
landed) to keep `docs/specs/` high-signal for agents and humans.

## What's here

- `specs/*-INSTRUCTIONS.md` (242) — per-phase build orders from v1 work streams
  (CM-PHASE-*, AGENTCTRL1-*, AUXILIUM-TIER-2-*, TOOLBAR-*, AUDITCLEANUP-*, …).
  Work orders, not contracts — their features shipped or were superseded.
- `specs/{file-tree,textview,runtime-terminal,gtk-container}-*` (4 subdirs) — phase
  bundles from the same era.
- `specs/` one-off work orders (42) — FIX-*, *-FINDINGS, *-AUDIT-REQUEST,
  *-COMPLETION-REPORT, UIRESP2-* verification rounds, STALE-PLAIN-TEXT-*, etc.
- `audits/`, `completed/`, `post-mortems/` — **not archived**; already foldered by
  convention and still referenced by living tests/comments.

## Keep rule (what stayed in docs/specs/)

Only spec masters (`SPEC-*`, `SPEC_*`, `spec-*`) and the `MEMRATCHET-*` chain (P11
memory-ratchet work — the v2 roadmap's first work unit builds on it).

## Why archive, not delete

Specs explain *why* code is shaped the way it is. When R5 rips the KB stack out of
`agent/runtime.py`, the AUXILIUM-TIER-2 orders in here explain what those branches were
for. Git history also retains everything; this folder is the browsable index.

## Note for v2 agents

Don't cite archived specs as authority for *new* work — they describe v1 decisions.
Load-bearing v2 references live in `docs/specs/` (masters), `docs/proposals/`
(DEVELCAKES-V2-CHANGE-LIST.md is the roadmap source of truth), and `.crabcakes/`
(requirements, architecture, work units).
