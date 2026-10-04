# SPEC-10 SP3 Fix Round — seed queue view on bar build + stale-card metadata + pm-only button disable

**Supervisor:** Supervisor; **Builder:** Coder; **Auditor:** Debugger
**Scope:** `ui/handlers/review_handler.py` + `ui/views/review_bar.py` +
`tests/test_review_queues.py` + `tests/test_review_bar_queues.py`.

---

## BUG #1 (MEDIUM) — bar built with non-empty queues renders no chips

`_show_review_bar` never seeds the region; `_refresh_queue_view` only runs
on mutations. Reachable via GAP-5b queue survival + tab reopen / mode
toggle (auditor's 5-step repro: `MODE-TOGGLE: pending=1 bar.set_queue_view
calls=0`).

### Fix

At the END of `_show_review_bar`, after `self._mc.set_review_bar(bar)`,
seed: `self._refresh_queue_view(project_name)`. (The idle_add ordering
already guarantees the bar is live.)

### Test

`test_show_review_bar_seeds_queue_view` — populate queues (2 agents),
build handler with a double main_content whose `get_review_bar` returns a
recording bar double, drive `_show_review_bar` directly, assert
`set_queue_view` was called with the pending list (pm included — the bar
renders pm read-only). RED today: 0 calls.

## BUG #2 (LOW) — stale-drop card lacks metadata["agent"]

The stale-drop emit (`review_handler.py:~1011`) passes no `metadata=`,
unlike the cap-drop and summary emits. A feed filtered by agent would lose
the stale card.

### Fix

Emit the stale card with `metadata={"agent": agent_key}` (in scope in the
per-agent loop).

### Test

Extend the metadata asserts to the stale-drop card (a stale-entry accept
scenario asserting the stale card's metadata["agent"]).

## SUGGESTION #3 (partial) — pm-only queue disables per-agent button

When only pm has entries, `Accept All (agent)` stays enabled with
`_selected_agent=None` → silent no-op on click.

### Fix

In `set_queue_view`: after rebuild, if no non-pm agent chip exists, set
the per-agent batch button `set_sensitive(False)` (re-enable otherwise).
Leave the two-layer pm-fallback and the double-lock cosmetic as-is
(registered).

### Test

`test_pm_only_disables_agent_batch_button` — pm-only view → per-agent
button insensitive; add one agent → sensitive again. (GTK sensitivity
assert under xvfb — mirror the existing view-test shapes.)

## Battery (paste all)

- `xvfb-run -a .venv/bin/python -m pytest tests/test_review_bar_queues.py tests/test_review_queues.py tests/test_review_handler_feed_card.py tests/test_review_state.py tests/test_review_log.py tests/test_stop_all.py -q`
- pyright 0 + ruff multisets on the two sources
- RED proofs (3)

## Do NOT change

- The refresh choke points, confirm flow, D4b states, the two-layer pm
  fallback, the double-lock cosmetic (all registered).

## COMPLETENESS (mandatory)

- [ ] BUG#1 seed + test — diff hunk + RED
- [ ] BUG#2 metadata + test — diff hunk + RED
- [ ] SUGG#3 button disable + test — diff hunk + RED
- [ ] Battery + baselines
- [ ] Related issues found, NOT fixed

Invoke `prompts/steelFramedCodeWriter.md`. Please write and report when done.
