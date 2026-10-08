# develcakes MVP — Comprehensive Integration Validation Plan

**Phase:** 5 (Testing) · **Supervisor:** Supervisor · **Builder:** Coder · **Auditor:** Debugger
**Date:** 2026-10-04 · **Baseline:** HEAD 469c7992 (all 11 MVP specs IMPLEMENTED)

## Purpose

Each of the 11 MVP specs passed its own adversarial audit **in isolation**. This
phase validates them **together** — the seams between subsystems were never
exercised as a whole. The deliverable is a set of cross-spec integration tests
plus an honest acceptance matrix re-verified end-to-end.

## What exists already (verified)

- 168 test files, ~4,430 tests, full suite green.
- Each subsystem has isolated unit/behavior tests: transcript store
  (zero-lost-writers), worktree manager, work leases, stop-all, review queues,
  enforcement, feed retention, render/sanitize, config migration, env divergence.
- **Gap:** no test drives a full **writer-turn lifecycle** across the seams
  (dispatch → lease/worktree → write → enforcement → transcript → checkpoint →
  review queue → accept), and no test drives **stop-all** across every subsystem
  it touches simultaneously.

## Integration seams to validate (the value of this phase)

| # | Seam | Specs joined | Risk |
|---|---|---|---|
| I1 | Writer turn end-to-end | 01+02+08+09+10 | worktree cwd vs transcript key vs checkpoint path vs queue attribution |
| I2 | Stop-all sweeps everything in flight | 09+08+10 | turn cancel + process kill + checkpoint abort + approval deny together |
| I3 | Provider error → feed card + transcript + no empty msg | 01+02+08 | error path must not persist empty assistant turn |
| I4 | Config-dir divergence → transcript store + migration | 08+11 | transcript.db lands in the NEW dir; migration copies the OLD one |
| I5 | Env fallback → runtime latch → migration gate | 01+11 | DEVELCAKES_/CRABCAKES_ resolution through the real import chain |
| I6 | Feed bounded memory under transcript-backed reload | 03+08 | live window + disk store + reload hydration |
| I7 | HTML sanitize at every chat call site | 06+02 | guard holds for error/system cards too |
| I8 | App identity + launcher + migration banner | 11 | `develcakes` boots, app id correct, banner on first run only |

## Acceptance matrix (re-verify end-to-end, not just per-spec)

All 11 specs' acceptance criteria are re-checked in one pass; the matrix below
records the integration-level evidence for each (the per-spec evidence is in
each spec's phase reports).

## Deliverables

1. `tests/test_mvp_integration.py` — cross-seam integration tests (I1–I8).
2. `docs/testing/MVP-VALIDATION-REPORT.md` — the acceptance matrix + results +
   any gaps found.
3. A post-mortem per the implementationLoop §6 format if any fix round occurs;
   otherwise a short validation report.

## Process

Standard loop: Coder writes integration tests (steelFramedCodeWriter), Debugger
adversarially audits them + probes for seams the tests miss, Supervisor verifies
independently, full battery at the end. Any production defect found → normal
fix-round loop.

## Non-goals

- Re-writing per-spec unit tests (they exist and pass).
- Live GUI manual testing (that's the ship-phase manual-test plans).
- Performance/load testing beyond the existing retention + concurrency pins.