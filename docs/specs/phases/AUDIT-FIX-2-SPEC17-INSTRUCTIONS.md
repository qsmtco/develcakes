# AUDIT FIX ROUND 2 — SPEC-17 SP1 gate hardening (Debugger re-audit 2026-10-07)

**Source:** Debugger post-fix re-audit. Both round-1 bugs verified FIXED (repros no
longer reproduce). The BUG#1 fix itself introduced a HIGH wedge class. Fix round 2 —
same scope, no restructuring.

**Files:** `ui/views/chat_surface.py` + `tests/test_chat_surface.py`. NO feed files
this round (BUG#2/feed is closed). Bars: chat ≥130, feed stays ≥276 (re-run once to
prove no regression), ruff baselines, pyright 0.

---

## BUG #1 (HIGH — introduced by round 1) — the wedge guard is inert on the real load path

**Proven by the auditor's probes:**
- `load_html(inline, "about:blank")` emits `STARTED → COMMITTED → FINISHED` —
  **`load-failed` NEVER fires** for the surface's only load path. The round-1
  `_on_load_failed` guard is dead code for content loads.
- `WebView.terminate_web_process()` (web-process crash) fires `web-process-terminated`
  and **neither** `load-failed` **nor** FINISHED → `_load_in_flight` sticks True →
  every later render defers forever. 2/5 robust trials wedged.
- A raising `_load_html` (`load_html(None, ...)` raises — probe-confirmed) leaves the
  flag armed (set BEFORE the call, nothing clears on the raise) → deterministic wedge.
- **Harm:** `_on_load_failed` ALSO clears `_pending_scroll` — but on a real failed
  *navigation*, `load-failed` is immediately followed by FINISHED (probe CASE B), so
  clearing the intent makes the FINISHED apply NOTHING (probe D: intent `(True, 900)`
  → `applies=0`).

**Fix (all four parts):**
1. `_issue_load`: wrap the `_load_html(doc)` call in `try/except Exception` →
   clear `_load_in_flight`, `logger.exception(...)` (BLE001 noqa + log, house
   idle-callback discipline — must not kill the render loop), and kick
   `_schedule_render()` if `_dirty` so the row is not stranded.
2. Connect **`web-process-terminated`** in `_ensure_webview` (arity `(view, reason)`
   — verify with a probe or GObject signal query; do not fabricate). Handler:
   clear `_load_in_flight` (+ clear the stale `_pending_scroll` — a crashed web
   process consumed nothing) and kick `_schedule_render()` if `_dirty`. Probe
   evidence already shows the WebView recovers on a fresh load.
3. `_on_load_failed`: **stop clearing `_pending_scroll`** (keep clearing the gate —
   the signal is real for failed navigations and harmless to keep).
4. `destroy()`: disconnect `web-process-terminated`.

**Tests (RED-first, from the auditor's probes):**
- `test_web_process_terminated_releases_gate_and_renders`: drive the real
  `web-process-terminated` signal → assert gate cleared AND a dirty row re-renders.
  (RED against current code: gate stuck.)
- `test_raising_load_releases_gate`: monkeypatch `_load_html` to raise → assert no
  escape from `_issue_load`'s caller, gate cleared, dirty row re-renders on the next
  drain. (RED: gate stuck / exception escapes the idle.)
- `test_load_failed_then_finished_still_applies_intent`: fire `_on_load_failed` then
  FINISHED with an intent stored → assert the apply script IS issued. (RED: current
  code cleared the intent → applies nothing.)

## BUG #2 (LOW) — orphaned dirty row: FINISHED early-returns before the kick

`_on_load_changed` (`:619-627`): the `if pending is None: return` runs BEFORE the
`if self._dirty: self._schedule_render()` kick → with `pending=None, dirty=True` the
row renders only on a future append (delayed, not lost).

**Fix:** reorder — clear gate → apply intent if any → kick if dirty. The kick must
run regardless of the intent's presence.

**Test (RED-first):** `test_finished_with_no_pending_still_kicks_dirty`: state
`_load_in_flight=True, _pending_scroll=None, _dirty=True, _render_pending=False` →
FINISHED → assert `_render_pending is True`.

## Issue #3 (LOW — route as required; it is a UX bug in the NEW SP1.3 method)

`scroll_to_latest` while a pre-load READ is in flight: the read callback overwrites
`_pending_scroll` with the stale position (probe B: `(False, 300.0)`), and the next
FINISHED applies `scrollTop=300` instead of the bottom — the user pressed
"go to latest" and is left mid-document.

**Fix (contract, implementation your choice):** after `scroll_to_latest()` arms the
intent, a completing in-flight read must NOT clobber it — the next FINISHED applies
`_BOTTOM_SCRIPT`. A monotonic intent-sequence guard or a button-override flag are
both acceptable; keep it O(1), no new state class.

**Test (RED-first):** `test_scroll_to_latest_wins_over_in_flight_read`: start a read
(don't complete it), press `scroll_to_latest`, complete the read with `(False, 300)`,
then FINISHED → assert the issued script is `_BOTTOM_SCRIPT`.

## Verification (paste ALL outputs)

```bash
xvfb-run -a .venv/bin/python -m pytest tests/test_chat_surface.py tests/test_chat_render_scroll.py tests/test_html_guard_sites.py -q   # ≥130
xvfb-run -a .venv/bin/python -m pytest tests/test_feed_handler.py tests/test_feed_retention.py -q                                      # ≥276 (regression proof)
ruff check ui/views/chat_surface.py tests/test_chat_surface.py --output-format concise | tail -2   # baselines: 0 / 0
pyright ui/views/chat_surface.py 2>&1 | tail -1                                                    # 0
```

Report: files+lines, RED proofs verbatim, all outputs, COMPLETENESS checklist (one
line per fix part + per test), related issues flagged not fixed. Word marker:
**please write**.
