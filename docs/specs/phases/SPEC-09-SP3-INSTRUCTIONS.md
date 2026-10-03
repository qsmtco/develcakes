# SPEC-09 SP3 — Stop-All: Popen Group-Kill + Turn Registry + Toolbar

**Spec:** SPEC-09 §2 stop-all + pre-flight D1=(b) (PM-approved: Popen +
start_new_session + process-GROUP kill, SIGTERM → 2s → SIGKILL).
**Builder rule:** `prompts/steelFramedCodeWriter.md` — invoke it, apply every rule.
**Depends on:** SP0–SP2 (6b3beb72, 19c680aa, 07efb6b2).
**Touches:** `agent/tools.py`, `agent/runtime.py`, `ui/handlers/agent_runtime_handler.py`,
`ui/toolbar.py`, `ui/window.py`, `ui/handlers/review_handler.py`, tests.

---

## Verified anchors (HEAD 07efb6b2)

- tools.py `:414` `subprocess.run(shell=True, capture_output, timeout, scrubbed env)`
  inside `_exec_command` (sig :380 — already carries `session_key`!). `:527`
  `_run_grep` (short timeouts, no session context — LEAVE AS-IS this phase).
- runtime: `cancel(sk)` :1172 (cancelled-set + `_cancel_requested` + approval
  deny-flush ALREADY per-session), `_dispatch_approval` wait :2124,
  `_active_loops` :606, `_turn_tokens` :621.
- ARH `_runtimes` :79. Toolbar buttons :30-67. Review checkpoint via
  `git_ops.commit(allow_empty=True)`.

## Edit 1 — tools.py: Popen + registry + group kill (D1=(b))

Module level:
```python
_PROCESS_REGISTRY: dict[str, list[subprocess.Popen]] = {}   # session_key -> procs
_PROCESS_REGISTRY_LOCK = threading.Lock()

def cancel_all_processes(session_key: str | None = None) -> int:
    """Group-kill registered processes (SIGTERM → 2s → SIGKILL). All sessions
    if None. Returns count killed. Escalation per process-group (start_new_session)."""
```

`_exec_command` rewrite (the :414 site ONLY):
- `Popen(command, shell=True, cwd=…, stdout=PIPE, stderr=PIPE, text=True,
  env=scrubbed, start_new_session=True)` — register under session_key's list
  (lock), then `communicate(timeout=timeout)`; finally: unregister + on
  TimeoutExpired: **group-kill the process** (same escalation) + communicate()
  reaper + return the timed-out ToolResult (today's shape).
- Output truncation, scrubbed env, cwd semantics: byte-identical contract.
- `cancel_all_processes` uses `os.killpg(proc.pid, SIGTERM)` → wait 2s →
  `SIGKILL` per group; never raises on already-dead (ProcessLookupError pass).

## Edit 2 — runtime.py: stop_all

```python
def stop_all(self) -> dict[str, str]:
    """Cancel every in-flight turn + kill every registered process. sk -> outcome."""
```
- Iterate `list(self._active_loops)` calling `self.cancel(sk)` (existing per-session
  machinery incl. approval deny-flush) — outcomes from cancel's bookkeeping.
- Then `tools.cancel_all_processes()` (ALL sessions — this runtime's scope).
- Order: cancel turns FIRST (token rotation) then kill processes (a killed
  process returns into a turn already marked cancelled — no zombie dispatch).

## Edit 3 — ARH: aggregate + review abort

- `stop_all_agents()`: iterate `self._runtimes.values()` → `rt.stop_all()`;
  merge outcome dicts; set `self._stop_all_in_progress = True` BEFORE the loop,
  clear after; emit ONE summary feed card (sessions cancelled, approvals denied,
  processes killed, checkpoints aborted — FeedCardData via the existing seam).
- Review abort: `review_handler`'s checkpoint path checks a stop flag BEFORE
  `git_ops.commit` — abort with a card instead ("checkpoint aborted: stop-all").
  Flag source: ARH passes a callable/attr the review handler reads (read the
  actual wiring between ARH and review_handler; do not invent a global).

## Edit 4 — toolbar ■ Stop All + confirm + wiring

- `■ Stop All` button (destructive styling) → `Gtk.MessageDialog` confirm →
  `stop_all_agents()` callback (toolbar ctor param, window wires it).
- No-op case: card "0 turns in flight" (spec §7).

## Edit 5 — tests (THE acceptance core)

tools.py (new file `tests/test_process_kill.py`, bare-safe where possible):
1. `test_exec_still_works` — basic exec round-trip via `_exec_command` (output,
   exit codes, truncation intact).
2. `test_timeout_group_kills_children` — `bash -c 'sleep 30 & wait'` style with
   a small timeout: after timeout, NO surviving process from the group
   (pgrep the sleep's distinctive marker).
3. `test_cancel_all_kills_running` — start `sleep 300` via _exec_command in a
   THREAD; call `cancel_all_processes(sk)`; the exec returns (killed), registry
   empty.
4. `test_escalation_sigkill` — a SIGTERM-ignoring process (`bash -c 'trap "" TERM;
   sleep 30'`): killed within ~3s (SIGKILL escalation).

runtime/ARH:
5. `test_stop_all_cancels_active_loops` — runtime with a fake in-flight turn
   (or real short send if the harness allows): stop_all → cancelled outcomes,
   `_active_loops` empty, approval events all set+denied.
6. `test_stop_all_denies_pending_approval` — a waiting `_dispatch_approval`
   unblocks as DENIED (no 60s hang) — the spec AC.
7. `test_stop_all_aborts_checkpoint` — review checkpoint under stop-all →
   aborted card, NO commit (git log unchanged).
8. **THE mid-tool-call halt (spec AC #1):** agent session starts
   `_exec_command("sleep 300")` (thread); stop_all lands MID-EXEC; assert:
   process dead (≤3s), turn terminates CANCELLED, approval waiters unblocked,
   summary card lists it. This is the ruling-#6 test — make it real, not mocked.

## Verification (paste full output — pyright MANDATORY, xvfb full suite)

```
env -u DISPLAY .venv/bin/python -m pytest tests/test_process_kill.py -v   (if bare-safe)
xvfb-run -a .venv/bin/python -m pytest tests/test_agent_runtime.py tests/test_agent_runtime_handler.py tests/test_review_handler.py tests/test_toolbar.py -q 2>/dev/null || true
.venv/bin/python -m pyright agent/tools.py agent/runtime.py ui/handlers/agent_runtime_handler.py ui/toolbar.py
python -m ruff check agent/tools.py agent/runtime.py ui/handlers/agent_runtime_handler.py ui/toolbar.py ui/handlers/review_handler.py
xvfb-run -a .venv/bin/python -m pytest tests/ -q -p no:cacheprovider 2>&1 | tail -3
```

## COMPLETENESS (mandatory)

- [ ] Edit 1: Popen + registry + group-kill escalation (exec contract byte-identical)
- [ ] Edit 2: runtime stop_all (turns first, then processes)
- [ ] Edit 3: ARH aggregate + stop flag + review pre-commit abort + summary card
- [ ] Edit 4: toolbar button + confirm + no-op card
- [ ] Edit 5: 8 tests incl. the mid-tool-call halt acceptance
- [ ] Full battery green incl. pyright + full suite
- Related issues found, not fixed: <list or none>

Deviations carry a one-sentence rationale each.
