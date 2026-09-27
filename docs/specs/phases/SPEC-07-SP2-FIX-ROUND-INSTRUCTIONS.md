# SPEC-07 SP2 FIX ROUND — audit findings BUG #1/#2/#3 (Debugger, 2026-09-27)

**Builder rule:** `prompts/steelFramedCodeWriter.md` — invoke it, apply every rule.
**Context:** SP2 production code is correct (Debugger: "clear to commit" on the code);
these findings close a coverage false-negative + dead code + stale docs BEFORE commit.
Debugger's mutation proof: three payload corruptions (drop state arg / wrong text /
constant state) survive all 172 targeted tests green — the phase's deliverable is
unverified. That gap closes now, not in SP4.

## BUG #1 (issue) — pin the `_update_status` (text, state) mapping

**Problem:** every surviving assertion checks `set_status_text.call_count` only; zero
assertions on ARGS. The 6-state text+state map — the SP2 deliverable — could be
entirely wrong and the suite stays green.

**Fix:** NEW parametrized contract test in `tests/test_activity_pill_adapter.py`
(SP2's pin file — keeps SPEC-07 pins together). Design:

```python
class RecordingTarget:
    """Records (text, state) pairs — the args-level fake BUG #1 demands."""
    def __init__(self):
        self.calls: list[tuple[str, str | None]] = []
    def set_status_text(self, text, state=None):
        self.calls.append((text, state))
    # progress quartet: no-op (handler may or may not call; neither is an error)
    def set_progress_fraction(self, f): pass
    def set_progress_hidden(self, b): pass
    def set_progress_pulse(self, e): pass
    def pulse_progress(self): pass
```

Drive the REAL ActivityHandler through each state via its public/state paths — do NOT
call `_update_status` directly; drive `_set_state(state, None)` per state (that's the
render trigger; `main_content=MagicMock()` so `_is_ui_active` passes, `GLib_module=fake_glib`
fixture per the existing pattern in tests/test_uirsp3_phase2.py).

Parametrize (state → expected_text):
- `idle` → `● Idle`
- `sending` → `⬡ Pre Flight Check`
- `reasoning` → `◉ Reasoning…`
- `tool_use` → `⚙ <tool name>` (set `handler._current_tool_name = "read_file"` first)
- `done` → `✓ Done`
- `streaming` → REGEX `^⬇ Generating… · \d+ tokens · \d+ tok/s · \d+\.\d+s$`
  (set `_streaming_token_count = 800` and `_agent_start_time` to a fixed monotonic
  via monkeypatched `time.monotonic` like tests/test_uirsp3_phase2.py:387 does, so
  the string is deterministic; OR assert via re.match on the regex — regex is fine)

Assert for EVERY state: the LAST recorded call is `(expected_text, state)` — the
state argument must be the exact state string, not None, not a constant.

**Mutation proof (mandatory, paste each):** re-run Debugger's three mutations against
the NEW test — each must go RED:
- MUT A: `set_status_text(text, state)` → `set_status_text(text)` (drop arg)
- MUT B: `text = "◉ Reasoning…"` → `text = "WRONG"`
- MUT C: `set_status_text(text, state)` → `set_status_text(text, 'idle')`
Restore after each; final state green.

## BUG #2 (suggestion) — delete the orphaned helper

- Delete `_compute_progress_fraction` (zero callers — verified repo-wide).
- Delete `self._progress_start_time` — after the helper goes it is write-only:
  remove the :72 declaration, the :570 write in `_reset_progress`, the :576 pop in
  `_reset_session_state`, and the :718 read dies with the helper.
- KEEP `_phase` and `_event_hop_count` — still read by `_live_update`'s signature.
- Gate: `grep -n "_compute_progress_fraction\|_progress_start_time" ui/handlers/activity_handler.py` → ZERO.
- Check tests: `grep -rn "_progress_start_time\|_compute_progress_fraction" tests/` — if
  any test references them, those assertions are stale-contract and must be repointed
  or retired per the subject-died rule (report each).

## BUG #3 (suggestion) — stale docstrings

Rewrite (keep the historical anchors — they explain the why):
1. `_status_tick` idle-branch comment (:743-746): "the bar is left in a clean
   hidden/idle state, never stranded mid-pulse" → "the idle budget is exhausted and
   the source dies cleanly (no render calls fire — the pill keeps its last state)".
2. `_live_update` docstring (:774): "markup rebuild" → "status rebuild" (plain text now).
3. `_idle_pulse` docstring (:796-799): rewrite to describe what it is NOW — the idle
   keep-alive decision (returns True while idle; the pulse render target died with
   the bar in SP2) — one short paragraph, keep the AC3/UIRESP3 history pointers.

## Verification (paste full output)

```
xvfb-run -a .venv/bin/python -m pytest tests/test_activity_pill_adapter.py tests/test_uirsp3_phase2.py tests/test_activity_bubbles.py tests/test_activity_drawer.py tests/test_missing_message_fix.py tests/test_chat_surface.py tests/test_chat_render_handler.py -q
env -u DISPLAY .venv/bin/python -m pytest tests/test_activity_pill_adapter.py -q   # stays bare-safe
#   (ActivityHandler is gi-free at module level — imports are logging/time only;
#    the ctor's lazy gi import never fires under the conftest fake_glib fixture.
#    VERIFY this still holds after your edits; if the new test trips gi, move it
#    to a xvfb-marked file and report the deviation.)
grep -n "_compute_progress_fraction\|_progress_start_time" ui/handlers/activity_handler.py   # ZERO
python -m ruff check ui/handlers/activity_handler.py tests/test_activity_pill_adapter.py     # baseline 9 + 0; report if differs
```

## COMPLETENESS (mandatory)

- [ ] BUG #1: parametrized (text, state) contract test — all 6 states pinned
- [ ] BUG #1: MUT A/B/C each proven RED against the new test (paste), restored, final green
- [ ] BUG #2: helper + `_progress_start_time` deleted — grep ZERO; stale test refs handled (report)
- [ ] BUG #3: three docstrings rewritten
- [ ] Full verification battery pasted
- Related issues found, not fixed: <list or none>

Deviations carry a one-sentence rationale each.
