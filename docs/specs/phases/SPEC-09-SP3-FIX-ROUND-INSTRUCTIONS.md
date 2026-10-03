# SPEC-09 SP3 FIX ROUND — audit BUG #1–#4 (Debugger, 2026-10-02)

**Builder rule:** `prompts/steelFramedCodeWriter.md` — invoke it, apply every rule.
**The auditor's probes (scratch/dbg_sp3_probe_a/b/c/e/g/h/i) are the spec —
reproduce each RED shape before fixing.**

## BUG #1 (HIGH — breaks AC #7) — escalation gated on leader, not group

`_group_kill`'s early `return` when the leader honored SIGTERM skips escalation
for surviving children; `cancel_all_processes`'s `proc.poll() is None` gate skips
signalling entirely when the leader already exited. Probe C is the acceptance
violation end-to-end: turn thread alive +6s, never CANCELLED, orphan holds the pipe.

**Fix (group-liveness probing — the auditor's shape):**
- `_group_kill(proc, grace)`: SIGTERM via killpg (ProcessLookupError → group
  gone → return True); sleep grace; probe `os.killpg(proc.pid, 0)` —
  ProcessLookupError → gone → True; else SIGKILL killpg (probe B proved it
  reaches orphans) → short wait → return whether the group is now gone
  (killpg(0) probe again; final True/False).
- `cancel_all_processes`: drop the `poll() is None` skip — call `_group_kill`
  unconditionally; the dead-group case raises ProcessLookupError harmlessly.
- The timeout path (:513-527) gets the same restructure.

**Tests (the auditor's demanded shapes — currently vacuous):**
- `test_term_ignoring_child_leader_honors` — `bash -c 'trap "" TERM; sleep
  300 <marker>' & wait` shape: leader dies on TERM, child ignores it → group
  dead within grace+escalation (≤3s), no survivor under /proc.
- `test_leader_exits_child_lingers` — `sleep 300 <marker> & echo started`
  (no wait): leader exits immediately, child lingers → cancel_all kills it
  (the dropped poll-gate case; probe I).
- Timeout-path variant of the first shape (probe H).

## BUG #2 (MEDIUM) — late-registered approval hangs 60s

Registration isn't cancellation-aware. Fix at the source: `_dispatch_approval`,
at registration, checks `session_key in self._cancelled` (under `_state_lock`
or whatever guards that set — read it) → resolve DENIED immediately
(result_ref[0] = None; event.set()) instead of waiting.
**Test:** the auditor's probe E — register AFTER stop_all returns → unblocks
<10s as DENIED.

## BUG #3 (LOW) — kill counts must be honest

`_group_kill` returns bool (group confirmed gone); `cancel_all_processes`
counts only confirmed kills; surface `N unkillable` when any group survives
(propagate into the ARH summary card — a surviving group after SIGKILL is a
hostile-process situation the PM must SEE, not a silent 1).
**Test:** simulate an unkillable group (mock `_group_kill` → False or a
killpg-permission error path) → count 0 killed + 1 unkillable in the card.

## BUG #4 (LOW) — pre-flight abort undercounts

The review PRE-FLIGHT gate (review_handler.py:227-232) doesn't call
`note_stop_all_aborted()`. Centralize: one `_abort_checkpoint_for_stop_all`
helper (emit + note) used by BOTH gates.
**Test:** pre-flight-gate abort (flag set before staging) increments the
counter — the card line present in the common case.

## Verification (paste full output — pyright MANDATORY, xvfb full suite)

```
env -u DISPLAY .venv/bin/python -m pytest tests/test_process_kill.py tests/test_stop_all.py -v
xvfb-run -a .venv/bin/python -m pytest tests/test_review_handler_feed_card.py tests/test_agent_runtime.py -q
.venv/bin/python -m pyright agent/tools.py agent/runtime.py ui/handlers/review_handler.py
python -m ruff check agent/tools.py agent/runtime.py ui/handlers/review_handler.py tests/test_process_kill.py tests/test_stop_all.py
xvfb-run -a .venv/bin/python -m pytest tests/ -q -p no:cacheprovider 2>&1 | tail -3
```

## COMPLETENESS (mandatory)

- [ ] BUG#1: group-liveness escalation (3 sites) + the 3 demanded test shapes (RED proof pasted)
- [ ] BUG#2: registration-time cancellation check + post-stop registration test
- [ ] BUG#3: honest counts + unkillable surfacing + test
- [ ] BUG#4: centralized abort helper + pre-flight counter test
- [ ] Full battery green incl. pyright + full suite
- Related issues found, not fixed: <list or none>

Deviations carry a one-sentence rationale each.
