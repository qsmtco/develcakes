# SPEC-42 Phase N3 — stop-all sweeps everything in flight

**Parent:** `docs/testing/MVP-INTEGRATION-VALIDATION-PLAN-v2.md` target N3.
**Baseline:** 46F / 5325P / 5S (+ any later additions). Failure set baseline: `/tmp/n12_fails.txt`.

## The question

When `stop_all_agents()` fires mid-flight, do turn cancel + approval deny +
checkpoint abort + process kill all happen together — nothing half-finished,
nothing orphaned, and a subsequent normal turn still works?

## Seam map (Coder-verified 2026-10-19, from the 148-call halt turn)

- `ARH.stop_all_agents()` (ui/handlers/agent_runtime_handler.py:413): sets
  `_stop_all_in_progress` → iterates `self._runtimes` (keyed by display
  name, e.g. "Coder") → `rt.stop_all()` per runtime → merges sk→outcome →
  reads+resets `_stop_all_aborted_checkpoints` → ONE summary card → clears
  flag in finally. Outcome grammar per session:
  `cancelled[+denied:N][+killed:N][+N-unkillable]`; then a global
  `cancel_all_processes(None)` sweep; then a final deny-flush popping every
  remaining `_pending_approvals` entry. Card body grammar: `Turns
  cancelled: N` / `Approvals denied: N` / `Processes killed: N` /
  `Processes UNKILLABLE: N` / `Checkpoints aborted: N`, else `0 turns in
  flight — nothing to stop`. Card metadata `origin == "stop-all"`.
- `AgentRuntime.stop_all()` (agent/runtime.py:1447): counts pending
  approvals (prefix match on sk) → `cancel(sk)` → `cancel_all_processes(sk)`
  per session.
- `cancel(sk)` (runtime.py:1390): arms `_cancelled_epochs.add(session_key)`
  — the DURABLE invalidation trace (`_cancelled` is discarded by the loop's
  own bookkeeping; the epoch is only cleared at turn start (:1690) and on
  COMPLETED/FAILED terminals (:868)).
- Checkpoint abort has TWO producers of the same counter:
  `ReviewHandler.start_review` (review_handler.py:314) — pre-flight gate +
  commit-boundary re-check after `git_ops.stage_all`, both routing to
  `_abort_checkpoint_for_stop_all` → `note_stop_all_aborted()`; AND the ARH
  agent-checkpoint path (arh :1520+, from `_do_response_complete`) with its
  own pre-flight + post-staging gates. Requires
  `rh.set_agent_runtime_handler(arh)` for the counter to reach the card.
- Processes: `exec_command` uses `start_new_session=True` (tools.py:739) so
  proc.pid is the pgid; `_group_kill` escalates TERM → 2s → KILL → 2s;
  group-confirmed-dead → `killed:N`; survived SIGKILL → `N-unkillable`.
- Approval wiring: `_dispatch_approval` (runtime.py:2532) registers
  `_pending_approvals[f"{sk}:{uuid8}"]`; the epoch guard refuses late
  registration. Denial surfaces as `None`.

## SUPERVISOR RULING (the assertion contract)

After stop-all, assert the session key is IN `runtime._cancelled_epochs`.
Do NOT assert ARH `_turn_tokens[sk]` was rotated — it is not (rotation
belongs to the next send, ui/agent_runtime/session.py:323). Secondary
optional assertion: a subsequent `send_message` gets a new token.

## The six tests (class TestN3StopAllSweeps in tests/test_mvp_integration.py)

1. Mid-flight turn (real `_run_loop`, `_call_llm` patched to return an
   `exec_command` tool call — provider seam only) → outcome contains
   `cancelled`, and `sk in runtime._cancelled_epochs`.
2. Pending exec approval → outcome contains `denied:N` (N>=1) AND the
   summary card body contains `Approvals denied: N`.
3. Review checkpoint mid-commit (drive `start_review` past pre-flight into
   staging under the flag, using the N1 `_wired` pattern) → no commit
   lands, `note_stop_all_aborted` incremented, card body contains
   `Checkpoints aborted: N`.
4. Registered killable process (a real `sleep 30` via the real exec path)
   → outcome contains `killed:1`.
5. Summary card emitted exactly once; the no-op case emits the
   `0 turns in flight` card.
6. AFTER stop-all: repo git status clean, no orphaned checkpoint commit or
   `agent/` branch created BY THE STOP-ALL PATH, and a subsequent normal
   turn completes end-to-end (reuse the N1 chain tail).

## Conventions

- Fakes: provider seam + a blocking sleep tool. NEVER mock
  `stop_all_agents`, `cancel`, or the ReviewHandler abort path.
- Reuse: the N1 `_wired`-style harness already in
  tests/test_mvp_integration.py; tests/test_review_queues.py doubles.
- Module owner: tests/test_mvp_integration.py (extend — it already has
  TestN1/TestN2 + the import block).

## Gate

- Targeted: `xvfb-run -a pytest tests/test_mvp_integration.py -q` pasted.
- Full suite BACKGROUNDED (nohup xvfb-run -a .venv/bin/pytest -q >
  /tmp/fulln3.log 2>&1 &), report `gate running pid N`, STOP — Supervisor
  polls. Baseline 46F/5325P/5S + N new, failure set diffed identical vs
  /tmp/n12_fails.txt.
- REAL DEFECT found? STOP mid-phase, report as a finding (fix round).
- ALSO: clear the 6 staged lint-red imports in tests/test_mvp_integration.py
  (json/subprocess/threading/SimpleNamespace/pytest/TurnResult+TurnStatus as
  applicable) — consume them in the new tests or drop them; the file must
  lint clean.
