# SPEC-09 Pre-Flight Verification + Decisions (2026-10-02, HEAD ef6c604b)

## A. Verified facts

1. **Runtime registry — as spec says.** `_active_loops: set[str]` (:606),
   `_turn_tokens` (:621), `_pending_approvals` (:592), `cancel(sk)` (:1172) with
   stale-token rejection (`_terminate_turn`). Handler aggregation surface exists
   (`_runtimes` dict, ARH:79).
2. **Approval waiters** — `_pending_approvals` structure confirmed; deny-flush via
   `result_ref[0]=False; event.set()` is implementable exactly as spec sketches.
3. **Review-layer abort** — checkpoint commit path (`git_ops.commit(allow_empty=True)`)
   confirmed at utils/git_ops.py:109; pre-commit gate on a handler flag is clean.
4. **worktree_manager.py — does not exist** (spec correctly marks NEW).
5. **work_persistence — zero lease/claim refs today** (spec correctly says "add").
6. **CRITICAL DEVIATION — tool executor uses `subprocess.run` (blocking), NOT Popen
   handles.** agent/tools.py :414/:527/:607: every exec runs
   `subprocess.run(shell=True, capture_output=True, timeout=N)`. The spec assumed
   "Popen handles tracked in a registry" — they are not; `subprocess.run` blocks the
   tool-loop thread with NO handle escaping to any registry. **A running
   `sleep 300` cannot be cancelled by anything except its own timeout.**
7. **Enforcement register item (context carry):** SPEC-06 left PATH-bleed +
   fake-venv enforcement vectors as a REQUIRED hardening round before SPEC-09's
   worktree work (test_enforcement.py carries the identity-gate pins). The spec's
   worktree integration routes agent exec through project_path = worktree — the
   enforcement identity gate (`_APP_ROOT` realpath check) is exactly what decides
   whether exec inside a WORKTREE (a different path than the app root) passes or
   fails. That interaction is untested and unspecified.

## B. The blocking decision — D1: how does stop-all reach a running subprocess?

The acceptance criterion (PM ruling #6: "stop-all covers mid-tool-call") requires
killing a process that `subprocess.run` is blocking on. Options:

- **(a) Popen conversion (spec's implied design, made explicit):** rewrite
  exec_command (and the other two run sites) to `Popen` + a module-level registry
  keyed by session_key + a wait-with-poll loop. stop_all → registry kills
  (SIGTERM → 2s → SIGKILL per spec §7). Cost: touching the hottest tool path
  (exec_command is the model's most-used tool); `subprocess.run` conveniences
  (timeout, capture) must be hand-rolled. Risk: med-high, exactly the kind of
  change that needs the full adversarial loop.
- **(b) Process-group kill via setsid:** launch with `start_new_session=True`
  (Popen still required) and kill the whole group — handles shell=True children
  (the shell forks; killing only the shell orphans children). Same rewrite cost
  as (a) but kills trees, not just the shell.
- **(c) Cooperative-only:** stop-all cancels turns at tool boundaries (next
  `_run_loop` check) and lets in-flight subprocesses run to their timeout; the
  acceptance criterion downgrades to "halts within max-timeout seconds."
  Cost: tiny. Betrays ruling #6's letter ("mid-tool-call included") for long
  timeouts (a 600s sleep blocks stop-all for 10 minutes).

**Supervisor lean: (b) — Popen + start_new_session + group kill.** It is (a) done
right: same registry design, but the kill escalates at the process-GROUP level so
shell children can't survive. This is the only option that satisfies ruling #6
literally for shell commands. NOT choosing (c): the PM's ruling was explicit and
the whole point of the spec.

## C. Riding decisions (lean included; overrule freely)

- **D2 — enforcement identity gate × worktrees:** the hardening round MUST land
  first (it's already the register order) AND the worktree phase needs an explicit
  gate decision: does exec inside `<repo>/.worktrees/<agent>` pass the identity
  gate? Lean: the gate checks "is the exec'd project the running app" to stop
  fake-venv self-tests — a worktree IS the app's source at a different path, so
  the gate needs a worktree-aware path check (realpath of worktree root ==
  realpath of repo root's worktree parent). This needs its own brief line, not an
  accident discovered in testing.
- **D3 — lease store shape:** WorkLease persisted INTO the unit record (spec) vs
  a sidecar `.crabcakes/leases.json`. Lean: in-unit (single atomic write, no
  cross-file consistency), TTL clock = time.time() with a monotonic guard note.
- **D4 — stop-all button placement:** toolbar (spec) next to Connect. Lean: yes,
  with confirm dialog (destructive) per spec.
- **D5 — ordering:** hardening round → SP1 leases → SP2 worktrees → SP3 stop-all
  (processes first: Popen conversion is the riskiest single change; landing it in
  SP3 isolates its blast radius) → SP4 close-out. Alternative: stop-all last also
  means the acceptance test (mid-tool-call halt) lands with the Popen conversion
  in one phase. Lean: SP3 combines Popen + stop_all + the halt test.

## D. Recommended phasing

- **SP0 (enforcement hardening — pre-req):** PATH-bleed + fake-venv vectors;
  worktree-aware identity-gate design (D2's decision encoded as tests).
- **SP1:** lease API (claim/release/assert, TTL, double-claim refused) + tests.
- **SP2:** worktree_manager (ensure/path/remove/list) + runtime cwd integration +
  identity-gate interplay tests.
- **SP3:** Popen conversion + process registry + group kill + runtime/handler
  stop_all + approval flush + toolbar button + review abort + THE mid-tool-call
  halt test.
- **SP4 (supervisor):** full battery, post-mortem, push.
