# SPEC-09 SP3 FIX ROUND 2 — re-audit: zombie liveness (HIGH), approval lifecycle (MED)

**Builder rule:** `prompts/steelFramedCodeWriter.md` — invoke it, apply every rule.
**Ruling (BUG#2):** the registration guard keys on a per-session cancellation
epoch that survives the loop's `_cancelled` bookkeeping — the loop may discard
its early-check set entry, but the epoch is cleared ONLY by `_terminate_turn`'s
terminal transition (the turn is over → no further registration is possible).
The auditor's probes (dbg_sp3fix_*) are the spec; reproduce each RED first.

## BUG #1 (HIGH) — zombie-only group reads alive; 4s dead-wait + false UNKILLABLE

`_group_alive` (killpg(pgid,0)) succeeds on an unreaped zombie leader. Reaping
runs only in the declared-dead branch. No-reaper windows (timeout path;
registration→communicate gap) spin both 2s windows and return False.

**Fix (the auditor's causation-proven shape):** `proc.poll()` at the TOP of
every loop iteration in `_group_kill` — both the SIGTERM-grace loop and the
post-SIGKILL loop — BEFORE `_group_alive`. poll() reaps; the zombie becomes
waitable-dead; killpg then sees the true (empty) group.
**Tests:** the auditor's three: (i) timeout-path e2e elapsed < 2.0s (was 5.01;
the existing <8.0 bound TIGHTENS to <2.0 — the auditor called 8.0
"defensible bound, wrong reason"); (ii) self-exiting leader →
`(killed, unkillable) == (1, 0)` fast; (iii) the no-reaper zombie shape
(zrate's 12/12 false-unkillable now reads (1,0) — assert it).

## BUG #2 (MED) — guard bypass + entry leak + approve_exec hijack

**(a) Guard:** add `self._cancelled_epochs: dict[str, int]` (or a set — epoch
count unneeded if monotonicity is enough; pick and document). `cancel(sk)` and
`stop_all` mark it; `_terminate_turn`'s terminal transition (under `_state_lock`)
clears it — nowhere else. `_dispatch_approval`'s registration guard reads THIS,
not `_cancelled`.
**(b) Leak:** the late-registration early-return POPS the entry it inserted.
**(c) Pre-existing flush-no-pop:** `cancel()`'s flush and stop_all's final
flush POP each entry they deny (`pop(key, None)` after set) — the dict cannot
accumulate; `approve_exec` can never resolve a phantom; `denied:` counts stay
honest.
**Tests (the auditor's three + the hijack):** (i) late registration after a
loop-triggered `_cancelled.discard` still denies <10s (the bypass probe);
(ii) N late registrations → `_pending_approvals` empty; (iii) PM
`approve_exec` click resolves the LIVE waiter when a stale entry existed
(hijack probe — post-fix there IS no stale entry; assert both the resolution
and the empty dict); (iv) the epoch clears on terminal transition — a session
that COMPLETED then re-sends registers approvals normally (no permanent
poisoning by the fix itself).

## BUG #4 (suggestion) + the 8.0s bound — fold in

The unkillable-card test keeps its grammar pin BUT gains a real-path sibling:
a genuine `_group_kill → False` unit (construct a group SIGKILL can't reap via
mock-at-Popen-level if a real unkillable process can't be built portably —
document the choice). The <8.0 timeout bound tightens per BUG#1(i).

## BUG #3 — REGISTERED (pre-existing), no code this round.

## Verification (paste full output — pyright MANDATORY, xvfb full suite)

```
env -u DISPLAY .venv/bin/python -m pytest tests/test_process_kill.py tests/test_stop_all.py -v
xvfb-run -a .venv/bin/python -m pytest tests/test_agent_runtime.py -q
.venv/bin/python -m pyright agent/tools.py agent/runtime.py
python -m ruff check agent/tools.py agent/runtime.py tests/test_process_kill.py tests/test_stop_all.py
xvfb-run -a .venv/bin/python -m pytest tests/ -q -p no:cacheprovider 2>&1 | tail -3
```

## COMPLETENESS (mandatory)

- [ ] BUG#1: poll-before-probe in both loops + 3 tests (tightened <2.0s bound; (1,0) shapes)
- [ ] BUG#2: epoch-cleared-only-at-terminal guard + pop-on-deny everywhere + 4 tests
- [ ] BUG#4: real-path unkillable sibling (or documented Popen-level choice)
- [ ] Full battery green incl. pyright + full suite
- Related issues found, not fixed: <list or none>

Deviations carry a one-sentence rationale each.
