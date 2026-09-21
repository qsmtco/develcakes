# AGENTCTRL1 Phase 1 — Implementation Instructions (Coder)

**Contract:** `docs/specs/SPEC-AGENT-CONTROL-1.md` (rev 2) — read IN FULL,
but implement **Phase 1 ONLY** (utils/status_report.py + scripts/crab_status.py
+ §2.1/§2.2/§2.3 invariants + §2.4 alert-state helpers) and **§7 Phase-1 test
rows**. Phase 2 (main.py/--nudge) and Phase 3 (auto-resume) are **OUT OF SCOPE
this phase** — do not touch main.py.

**Anchor drift:** the spec was authored against an older tree; verify anchors
by identifier, not line number. Key verified anchors (2026-09-12, tree at
d51b71a): `TurnStatus` enum agent/runtime.py:121 (RUNNING/STREAMING non-terminal;
COMPLETED/FAILED/CANCELLED terminal); `get_turn_state` :1995, `get_last_turn_result`
:1970; `save_conversation_to_disk` non-atomic at agent/persistence.py:92
(`open(path, "w")` → json.dump — the torn-file tolerance requirement is real);
`conversations_dir()` agent/persistence.py:28; `~/.config/crabcakes/audit-log.jsonl`
fields verified live: `{approved, args_hash, exit_code, result_hash, timestamp,
tool_name, user}`; `/var/crash/*.crash` exists on this box.

**Phase 1 module shape** (spec §2.1 governs the full API; this is the
required public surface, name it EXACTLY like this):

```
utils/status_report.py
  collect(project_path=".") -> dict        # the report dict (see §2.1 structure)
  render_text(report) -> str
  render_json(report) -> str               # json.dumps(report, indent=2)
  assess(report, now=None) -> tuple[str, int]  # (worst_class, exit_code)
  STALL_THRESHOLD_MINUTES = 10             # module const, used by assess()
```

Public surface for the CLI shim scripts/crab_status.py: --project PATH,
--json, --full, --no-feed, --watch N, plus --auto-resume/--watch reserved
flags for Phase 3 — **implement only** --project/--json/--full/--no-feed/--watch
this phase; --auto-resume prints "not implemented (Phase 3)" to stderr and
exits 10 if passed.

--watch N: loop sleep(N) re-render, Ctrl-C exits 0. Exit codes per spec §2.2:
0 healthy / 2 attention / 3 app-not-running / 10 reserved-flag.

**Content policy (§2.1, PM decision §11.3):** message bodies truncated to
120 chars by default; --full raises BODIES only to 2000 chars; **tool args
and exec commands/outputs never rendered in full at ANY verbosity** — only
tool names. Your tests must assert tool-args secrecy at --full too.

**Caching:** cache the feed summary to `<project>/.crabcakes/cache/status-feed.json`
keyed by (path, size, mtime); skip re-parse on hit. Never take the feed flock.
Cache dir creation must be mkdir -p, chmod 0o700.

## Phase 1b — alert-state helpers (§2.4, cron alerting)

Same module, second public surface (no scheduler wiring — the PM wires cron):

```
  load_alert_state(state_path) -> dict     # missing/corrupt -> {} (tolerant)
  save_alert_state(state_path, state)      # atomic write (tmp+os.replace), 0o600
  episode_id(session_key, last_ts, last_sha) -> str   # sha256(...)[:16]
  should_alert(state, episode, now, realert_hours=1.0) -> tuple[bool, dict]
      # True on state change or re-alert floor elapsed; returns updated state
      # (mutate a copy, don't surprise the caller) — document the contract
  mark_alerted(state, episode, now) -> dict  # returns updated state
```

These are pure functions over a state dict; no scheduler, no daemon.

## PHASE SPLIT (Supervisor ruling — 1 file per phase)

**Phase 1a** = `utils/status_report.py` ONLY: `collect()`, `render_text()`,
`render_json()`, `assess()`, `STALL_THRESHOLD_MINUTES`, the 5 stall classes,
content policy (§11.3), feed-summary cache, episode/alert-state helpers
(load_alert_state / save_alert_state / episode_id / should_alert / mark_alerted).
Plus `tests/test_status_report.py` — the §7 Phase-1 rows + 3 hermeticity tests,
~20-25 tests total, ALL under tmp_path with monkeypatched HOME. RED-first
against the nonexistent module.

**Phase 1b** = `scripts/crab_status.py` ONLY: argv parsing (--project, --json,
--full, --no-feed, --watch N; --auto-resume → stderr "not implemented
(Phase 3)", exit 10), exit-code mapping (0/2/3/10), report rendering to
stdout, --watch loop. Plus `tests/test_crab_status_cli.py` — the §7 argv rows
(test_argv_parse_nudge_forms → refusal/exit-10 on dispatch attempt,
test_readonly_without_auto_resume_flag) + exit-code mapping tests. RED-first.
Depends on 1a landing first.

**Spec-gap rulings (spec §2.1/§2.4 were silent; supervisor decides per
loop Rule 2, flag in report if you disagree):**

1. **Cache/state home:** spec says `<cache>/crabcakes/…` without defining
   `<cache>`. Ruling: `os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")) + "/crabcakes"` — NOT under the project (the report serves
   multi-project monitoring; the spec's `<project>/.crabcakes/cache` framing
   in my earlier draft was wrong). Tests must monkeypatch XDG_CACHE_HOME.
2. **`--no-feed`:** report still runs the conversation/git/crash/audit
   sections; only the feed summary is skipped. Exit-code unaffected by feed
   (feed problems are never a stall class).
3. **`app_running`:** `/proc/*/cmdline` scan for `main.py` — cwd and
   cmdlin match the project path. No psutil dependency. `/proc` unreadable →
   app_running=False + `app_running_degraded: true` flag in the report (loud,
   per spec invariant 4).
4. **`turn_stalled` no-writes check:** "no repo writes since the message" =
   no file under project_path (excluding .git, .crabcakes) with mtime >= the
   last assistant message timestamp. Implement as a bounded os.walk (cap
   ~5k files, note truncation in the report if hit — loud degradation).
5. **§2.4 alert dedupe JSON shape:** `{"episodes": {id: {"first_seen", "last_alert", "attempts"}}, "last_exit": 0}` — should_alert/mark_alerted
   operate on this dict; JSON top level exposes `alert: bool` for the cron
   alert condition (dedupe already applied via should_alert).
6. **Blocked_on_sendback:** glob `docs/specs/*SENDBACK*.md` (any case) newer
   than newest commit; commit-newer check via `git_ops.log(project, 1)`.

Both phases: NO main.py/agent/ changes. 1b imports 1a only.

## §7 test rows IN SCOPE (split across 1a/1b as noted above)

test_report_healthy_exit0, test_turn_stalled_detected, test_blocked_on_sendback_detected,
test_report_never_acquires_feed_lock (monkeypatch feed_store._acquire_lock to raise),
test_feed_summary_cached_by_mtime (second call does not re-parse),
test_content_truncated_by_default, test_full_flag_raises_message_cap_only,
test_alert_deduped_by_episode, test_argv_parse_nudge_forms (argv parsing only
— the nudge FORMS parse; dispatch is Phase 2, so assert refusal/exit-10 on
dispatch attempt), test_readonly_without_auto_resume_flag, plus 3 hermeticity
tests: cache-under-0700, state-file-0600-atomic, report-never-writes-outside-project-cache.

## Environment / gates

- Headless suite: NO GTK in import path. `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_status_report.py -q` (plain, no xvfb — that's the POINT of this module; assert-import-green both ways)
- pyflakes /tmp/pf-venv3: 0 undefined on both new files
- RED-first: write the §7 rows against the nonexistent module first (import error = red), capture, then implement
- Hermeticity: everything under tmp_path; never touch real ~/.config/crabcakes or the running app's files. Use monkeypatch for HOME-dependent paths (config dir resolution)
- One commit: `feat(status): crabcakes status reporter — stall detection, feed summary, alert-state helpers (AGENTCTRL1 Phase 1)`
- COMMIT BOTH new files + the test file. Leave main.py, provider yaml, cron untouched.
- Report: COMPLETENESS checklist + pasted red/green evidence + grep proof that no main.py/agent/ changes exist (`git show --stat`)

STOP after commit. Debugger audits next; Unit C Phase 2+3 require separate PM sign-off.
