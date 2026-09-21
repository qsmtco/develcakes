# AGENTCTRL1-PHASE-1B-PLUS — Instructions (Coder)

**Goal (PM-approved):** wire the §2.4 alert-dedupe helpers into the CLI so
`crab_status.py --json` actually reports `alert: true/false`, and make cron
usable. Currently `should_alert`/`save_alert_state` have **no production call
site** — `report["alert"]` is always `False` and `status-state.json` is never
written. Supervisor-verified (tree at 605b194):

- `utils/status_report.py` — `episode_id(session_key, last_ts, last_sha)`,
  `should_alert(state, episode, now, realert_hours=1.0)` → `(bool, state)`,
  `mark_alerted(state, episode, now)`, `load_alert_state(path)`,
  `save_alert_state(path, state)`, `ALERT_REALERT_HOURS = 1.0`, `state_path()`.
- `scripts/crab_status.py` — `--auto-resume` reserved → stderr + exit 10;
  `EXIT_USAGE = 64`; exit codes 0/2/3.
- `scripts/crab_status.py:173` — `TODO(AGENTCTRL1 P1 audit BUG #9)` (watch-json).
- Unit C rulings (context.md): dedupe wiring deferred to this phase; episode
  identity was specced only for `turn_stalled` (§4.4) — the extension below is
  the supervisor's ruling for the other classes.

## DEFERRED — OUT OF SCOPE, do NOT implement

- **Phase 3 auto-resume** (`--auto-resume`): PM ruled "later". Leave the stub
  exactly as-is (stderr + exit 10), and leave `G1`–`G12` unimplemented.
- `--watch --json` framing (BUG #9): keep the TODO.

## EDITS

**Edit A — episode identity for ALL stall classes (`utils/status_report.py`).**
Add `stall_episode_id(stall: dict) -> str`, generalizing §4.4:
- `turn_stalled`: existing formula — sha256(session_key + last_message_ts + last_message_sha)[:16].
- `blocked_on_sendback`: sha256("sendback" + newest SENDBACK filename + its mtime)[:16].
- `app_spinning` / `app_frozen`: sha256(class + app pid + starttime)[:16] (stable
  while the same process stays in the same posture).
- `crash_after_start`: sha256("crash" + crash filename)[:16].
- `approvals_pending`: sha256(class + project_path)[:16].
Document that stability across invocations is the contract (a new episode must
appear the moment the underlying condition changes). Pure function, tolerant
input (missing keys → fall back to a class-only hash, never raise).

**Edit B — wire dedupe into the CLI (`scripts/crab_status.py`).**
- Compute the worst-class / alert condition from `assess()`'s output.
- Load state via `load_alert_state(state_path())` (tolerant: missing/corrupt → {}).
- If a stall class is present: compute its episode id (Edit A) and call
  `should_alert(state, episode, now, ALERT_REALERT_HOURS)`. Set
  `report["alert"]` from that result. On True, call `mark_alerted` and
  `save_alert_state`. On False (dedupe suppressed), `alert` stays False.
- Healthy run (exit 0): `report["alert"] = False` and do NOT write state
  (silence is the default — §2.4).
- **Read-only invariant stays:** without `--auto-resume`, the ONLY permitted
  writes are the cache file and the state file (§2.3.6 pins read-only for
  everything else). Do NOT change exit codes.
- **BUG #6 follow-through (already coded):** usage errors exit 64 — ensure the
  dedupe path can never turn a usage error into an alert.
- Expose the alert summary so cron doesn't reconstruct it: `report["alert"]`
  (bool) plus reuse the existing `report["summary"]` / `report["exit_code"]`
  (added in the audit fix round). If summary needs an alert-specific string,
  add `report["alert_summary"]` rather than overloading `summary`.

**Edit C — tests (RED-FIRST), new file `tests/test_status_alert_wiring.py`**
(+ extend the existing suites where a helper belongs):
1. `test_healthy_run_sets_alert_false_and_writes_no_state` — no state file created.
2. `test_stall_run_sets_alert_true_and_persists_state` — state file written, 0600.
3. `test_same_episode_within_floor_is_deduped` — second invocation → `alert` False,
   no re-alert (the whole point: no 96-messages/day nuisance).
4. `test_new_episode_alerts_again` — episode id changes → alert True.
5. `test_re_alert_after_floor_elapses` — advance `now` past the floor → True.
6. `test_episode_id_stable_across_invocations_and_changes_on_new_activity` — per class.
7. `test_stall_episode_id_tolerates_missing_keys` — no raise, class-only hash.
8. `test_auto_resume_stub_unchanged` — exit 10, stderr text, no state write.
9. `test_usage_error_exit_64_never_alerts` — usage error → 64, `alert` not True,
   no state written.
All hermetic: `XDG_CACHE_HOME`/`HOME` monkeypatched to tmp_path; NEVER touch the
real `~/.cache/crabcakes` or `~/.config/crabcakes` (Unit C + Phase 2 both had
hermeticity incidents — use a module-scoped autouse fixture that redirects the
state/cache path).

## GATES

- RED-first: paste the failures for tests 1–5 (they need the wiring).
- GREEN: new file green ×2; existing `tests/test_status_report.py` +
  `tests/test_crab_status_cli.py` still green (69 → 69+new).
- pyflakes /tmp/pf-venv3: 0 undefined on touched files.
- Live smoke (document commands + output): `scripts/crab_status.py --json`
  on a healthy project → `alert: false`; confirm no `status-state.json` is
  created. Then force a stall fixture if cheap, else state that tests cover it.
- Prove the running app's real files were untouched:
  `ls -la ~/.cache/crabcakes/` before/after — report.
- Full suite → **0F** (baseline 0F/3646P). Report count.
- One commit: `feat(status): wire §2.4 alert dedupe — episode ids per stall class + state file (AGENTCTRL1 Phase 1b+)`
- Flag (don't fix) anything adjacent.

Then STOP — audit next. The PM will add the cron entry themselves.
