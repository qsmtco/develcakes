# Phase 5 — MVP Integration Validation (I1–I8)

**Plan:** `docs/testing/MVP-INTEGRATION-VALIDATION-PLAN.md`
**Prompt:** `prompts/steelFramedCodeWriter.md`
**Supervisor:** Supervisor; **Builder:** Coder; **Auditor:** Debugger
**Baseline:** HEAD 469c7992 (11 MVP specs IMPLEMENTED)

---

## Mission

Write `tests/test_mvp_integration.py` — cross-subsystem integration tests for the
8 seams in the plan. The per-spec unit tests exist and pass; this file proves the
subsystems work **together**. Plus `docs/testing/MVP-VALIDATION-REPORT.md`.

**Reuse, don't rebuild:** `tests/test_agent_runtime.py::TestSpec10AgentCheckpoints`
already builds ARH + ReviewHandler + a real git repo + a live token
(`_wired`, `_finish`, `_spec10_rh`). `tests/test_review_queues.py` has the queue
doubles (`_make_handler`, `_entry`, `_activate`, `_wait_until`). Import/copy
those patterns — disclose which you reused vs. re-derived.

## The 8 integration tests (RED-first where a seam is unproven)

### I1 — Writer turn end-to-end (01+02+08+09+10)
One test driving the full lifecycle on a real git repo:
1. lease the agent (SP1 work lease) → `_worktree_for_turn` returns a worktree
   path under `<proj>/.worktrees/<id>`;
2. the conversation's write cwd = the worktree;
3. a completed writer turn (COMPLETED token) → checkpoint committed **in the
   worktree** with the `Agent:` trailer;
4. the queue entry's `path_used` == the worktree path;
5. `accept_agent_queue` marks it reviewed (no duplicate commit);
6. **the transcript store received the turn** (or assert the seam that connects
   them — read how the runtime persists turns: `agent/persistence.py` +
   `utils/transcript_store.py`).
If (6) requires a runtime-level harness beyond ARH, build the minimal real
runtime path — do NOT mock the transcript store (it's the seam under test).

### I2 — Stop-all sweeps everything in flight (09+08+10)
Drive `stop_all_agents` while: a writer turn is mid-flight (fake a slow tool),
a checkpoint is in progress, an approval is pending. Assert: turn cancelled,
checkpoint aborted (counter honest), approval denied, **transcript has no
partial turn** (or the documented rollback), no queue entry for the aborted
checkpoint.

### I3 — Provider error path (01+02+08)
A provider error mid-turn → feed card emitted (SPEC-02) AND **no empty assistant
message persisted** (transcript row count unchanged) AND the error is recorded
in the transcript if SPEC-08 says so (read the spec's error contract first —
verify, don't assume).

### I4 — Config-dir divergence (08+11)
`get_config_dir()` → new dir; `TranscriptStore()` default path lands in the NEW
dir; `migrate_v1_config()` copies an OLD-dir `transcript.db` (+wal/shm) into the
new dir; the migrated DB opens and returns the same rows (byte-copy integrity).

### I5 — Env fallback through the real chain (01+11)
Subprocess: `CRABCAKES_MIGRATE_STORE=0` → import main → runtime latch False;
`DEVELCAKES_MIGRATE_STORE=0` → False; neither → True. (Extend the SP2 subprocess
pattern; assert through the real import graph.)

### I6 — Feed bounded + transcript reload (03+08)
Append beyond the live window (harness from `test_feed_retention.py`); assert
live widget count bounded; reload from the disk store hydrates the tail +
"N earlier" row. (If SPEC-08's store-mode reload path is the source, join it.)

### I7 — Sanitize at every chat call site (06+02)
Every path that renders chat content (assistant text, error cards, system
cards) routes through `render/sanitize.py`. Guard test: a script/iframe
payload in an ERROR card is neutralized, not just in normal assistant text.

### I8 — App identity + banner (11)
Source/behavior: `develcakes` launcher, app id `com.develcakes.app`, migration
banner emitted once on first activate, silent on second (marker).

## Rules

- **Real seams, not mocks.** Mock only true externals (network, subprocess,
  clock). The transcript store, worktree manager, queue, feed store are the
  code under test — use real instances on tmp dirs.
- **Every test must be able to fail** (Rule 4). For each seam, name the mutation
  that would break it; if you can't, the test isn't pinning the seam.
- **Read before writing** — read the actual persistence/transcript/worktree/
  review/feed code to get the real APIs. Do not fabricate.
- If a seam reveals a **production defect**, do NOT fix it silently — report it
  in the COMPLETENESS block; the Supervisor routes a fix round.

## Deliverables

- `tests/test_mvp_integration.py` (I1–I8, RED-first where unproven)
- `docs/testing/MVP-VALIDATION-REPORT.md`: the acceptance matrix (all 11 specs'
  criteria re-checked with integration-level evidence) + I1–I8 results + any
  gaps found.

## Battery (paste all)

- `xvfb-run -a .venv/bin/python -m pytest tests/test_mvp_integration.py -q`
- The 3 review + transcript + worktree + stop_all + feed_retention suites
- ruff/pyright vs baselines on new files
- RED proofs for each integration test

## COMPLETENESS (mandatory)

- [ ] I1 writer-turn e2e — evidence + which doubles reused
- [ ] I2 stop-all sweep — evidence
- [ ] I3 error path — evidence
- [ ] I4 config-dir + transcript migration — evidence
- [ ] I5 env fallback chain — evidence
- [ ] I6 feed bounded + reload — evidence
- [ ] I7 sanitize error-card path — evidence
- [ ] I8 identity + banner — evidence
- [ ] RED proofs — evidence per test
- [ ] Validation report — file written
- [ ] Related issues / production defects found — flagged (NOT fixed)

Invoke `prompts/steelFramedCodeWriter.md`. Please write and report when done.