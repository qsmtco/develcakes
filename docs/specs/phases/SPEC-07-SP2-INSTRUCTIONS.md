# SPEC-07 SP2 — ActivityHandler Repoint to the Pill (R4 feedbar removal, 2 of 3)

**Spec:** `docs/specs/SPEC-07-R4-FEEDBAR-REMOVAL.md` §2 AMENDED (read it first — it
supersedes the original sketch; "FeedBar" there names the old protocol, not a kept widget).
**Builder rule:** `prompts/steelFramedCodeWriter.md` — invoke it, apply every rule.
**Depends on:** SP1 landed (`ActivityPillAdapter`, `set_activity_status`, `surface_for_key` exist).
**Touches:** `ui/handlers/activity_handler.py`, `ui/window.py`, four test files.
**Does NOT touch:** feedbar.py, chat_surface.py, chat_render_handler.py.

---

## Goal

Repoint ActivityHandler's render half from the FeedBar widget to the SP1 pill adapter.
The state machine (states, timers, budgets, ticker, skip-gating signature) is UNTOUCHED —
only the render target, its call shapes, and label text change.

Verified facts (HEAD 3972ac9e, post-SP1):
- `activity_handler.py:39` — `def __init__(self, feedbar, main_content, GLib_module=None)`
- `self._feedbar` render calls: `set_progress_hidden` (~:683), `set_progress_fraction` ×4
  (~:687-:700), `set_status_text` (~:706), `set_progress_pulse(False)` in `_stop_idle_pulse`
  (~:776) and `_status_tick` idle branch (~:831 area), `set_progress_hidden(True)` in the
  same branch, `pulse_progress()` in `_idle_pulse` (~:831).
- `_update_feedbar` defined ~:676; called at the same-state early return (~:624), inside
  `_set_state` (~:665), and in `_live_update` (~:820); referenced in comments ~:351-:355.
- `window.py`: import :36, `self._response_status = FeedBar()` :256, handler ctor with
  `feedbar=` :259-263, `right_box.append(self._response_status)` :773.
  `self._chat_render_handler` is built at :124 — BEFORE the activity handler; safe to
  reference inside a lazy lambda.
- Test repoint surface: `ActivityHandler(feedbar=…)` / `h._feedbar` —
  test_activity_bubbles.py (47 refs), test_uirsp3_phase2.py (1 fixture + widget tests),
  test_activity_drawer.py (5), test_missing_message_fix.py (14).

## Edit 1 — ctor + attribute rename (mechanical)

- Ctor signature: `def __init__(self, status_target, main_content, GLib_module=None):`
- `self._feedbar = feedbar` → `self._status_target = status_target`
- Update the module docstring's "Response Status bar (FeedBar)" language to name the
  activity pill + status_target duck-type (SPEC-07 R4).
- Nothing else. Straight rename.

## Edit 2 — `_update_feedbar` → `_update_status`, plain-text labels

Rename the method and ALL references (calls at ~:624, ~:665; the disabled phase-2 hop
comment ~:351-:355 — update the method name inside the comment; `_live_update` ~:820).

New body, verbatim (markup stripped — the pill label is plain text and the CSS class
comes from the `state` argument):

```python
    def _update_status(self):
        """Update the activity pill (plain text + state) to reflect current state."""
        state = self._state
        if state == "idle":
            text = "● Idle"
        elif state == "sending":
            text = "⬡ Pre Flight Check"
        elif state == "reasoning":
            text = "◉ Reasoning…"
        elif state == "streaming":
            text = self._streaming_label()
        elif state == "tool_use":
            text = f"⚙ {self._current_tool_name}"
        else:  # done
            text = "✓ Done"
        self._status_target.set_status_text(text, state)
```

Notes:
- The tool branch does NOT escape the tool name: `Gtk.Label.set_text` (which the pill
  uses via `set_activity_status`) never parses markup, so escaping would display
  literal `&lt;`. `_escape_markup`'s only caller was the old markup branch — DELETE
  `_escape_markup` entirely and note the deletion in your checklist.
- The old body's `set_progress_hidden(True)` (idle branch) and the four
  `set_progress_fraction` calls are render calls to a progress element the pill does
  not have — they vanish with the rewrite (see Edit 3 for the rest).

`_streaming_label()` — same logic, plain text:

```python
        return f"⬇ Generating… · {token_est} tokens · {vel_str} · {elapsed_str}"
```

## Edit 3 — delete the remaining progress render calls

In `_stop_idle_pulse`: delete the `self._feedbar.set_progress_pulse(False)` line.
In `_status_tick`'s idle-budget-exhaustion branch: delete the
`set_progress_pulse(False)` and `set_progress_hidden(True)` lines (keep the
bookkeeping clears and `return False`).
In `_idle_pulse`: delete the `self._feedbar.pulse_progress()` call — the method keeps
its state check and `return True` (it is still the pulse branch of the ticker; with no
bar to pulse it is a no-op branch, and its budget semantics are unchanged and pinned
by test_uirsp3 rows 1-3).

The adapter retains its 5-method duck-type (SP1) for contract stability; the handler
simply no longer exercises the progress quartet.

Gate: `grep -n "set_progress\|pulse_progress" ui/handlers/activity_handler.py` → ZERO.

## Edit 4 — window.py repoint (lazy resolver)

Add with the other `ui.views` imports: `from ui.views.chat_surface import ActivityPillAdapter`.

Replace the :259-263 construction:

```python
        self._activity_handler = ActivityHandler(
            status_target=ActivityPillAdapter(
                lambda: (
                    self._chat_render_handler.surface_for_key(
                        self._main_content.get_current_session_key()
                    )
                    if self._chat_render_handler is not None
                    else None
                )
            ),
            main_content=self._main_content,
            GLib_module=GLib,
        )
```

The lambda MUST be lazy — never resolve the surface at construction time. Leave
:36 (import), :256 (construct), :773 (append) untouched — they die in SP3.
Gate: `grep -n "feedbar\|_response_status" ui/window.py` → exactly 3 lines (:36, :256, :773).

**Project-tab ruling (SP1 audit forward-risk 1, adjudicated):** surfaces are cached by
RENDER key (agent key), but `render_welcome("project:<name>")` creates a surface keyed
`project:<name>` mounted in the project box, and agent surfaces mount into that same box
via `mount_key`. On a project tab, `surface_for_key("project:<name>")` returns the
project's PRIMARY (topmost) surface — that pill is the tab's status pill; agent surfaces'
pills below stay untouched. This matches per-tab activity semantics. In SP2 you must:
(a) keep the resolver as `surface_for_key(get_current_session_key())`; (b) add ONE pin
test: project-tab key resolves to the welcome surface, agent key resolves to the agent
surface (two keys, two distinct surfaces — prove the resolver picks per-key). The
multi-surface pill display (N pills visible on a project tab, non-primary stuck on
"Idle") is a REGISTER item for SP3/SP4 disposition — do not redesign here.

**Streaming markup note (SP1 audit forward-risk 2):** already covered — Edit 2's
`_streaming_label` IS the plain-text conversion; do not reintroduce Pango spans.

## Edit 5 — four test files (mechanical repoint)

- All ~56 ctor sites: `feedbar=` → `status_target=`; local names `feedbar` →
  `status_target`; `h._feedbar` → `h._status_target`. Method-name assertions
  (`set_status_text.call_count` etc.) survive unchanged.
- `tests/test_uirsp3_phase2.py` line 76: `feedbar.set_progress_pulse.assert_any_call(False)`
  asserted a bar-cleanup call Edit 3 deleted. Replace that ONE assertion with nothing —
  the test's remaining asserts (seen[:19] all True, seen[19] is False, no re-arm,
  `_idle_ticks >= 20`) are the budget contract. Note the change in your checklist.
- DO NOT delete the FeedBar widget tests in test_uirsp3_phase2.py (rows 4, the two
  `set_progress_opacity` tests, the two `set_status_text` dedupe tests) — the widget
  still exists through SP2 and those tests stay green untouched. Their retirement is
  SP3 scope with disposition.

## Verification commands (paste full output)

```
python -m pytest tests/test_uirsp3_phase2.py tests/test_activity_bubbles.py tests/test_activity_drawer.py tests/test_missing_message_fix.py -v
python -m pytest tests/test_activity_pill_adapter.py tests/test_chat_surface.py -v
python -m pytest tests/ -q --ignore=tests/test_enforcement.py --ignore=tests/test_mcp_config.py -p no:cacheprovider 2>&1 | tail -5
python -m ruff check ui/handlers/activity_handler.py ui/window.py tests/test_uirsp3_phase2.py tests/test_activity_bubbles.py tests/test_activity_drawer.py tests/test_missing_message_fix.py
```

(The two ignored files are the known pre-existing env-broken reds, dispositioned in
SPEC-06 SP6 P2 — not this phase's problem.)

## Greps for your report

```
grep -n "_feedbar\|feedbar=" ui/handlers/activity_handler.py          # ZERO
grep -n "set_progress\|pulse_progress" ui/handlers/activity_handler.py # ZERO
grep -n "feedbar\|_response_status" ui/window.py                       # exactly 3
grep -c "status_target" tests/test_uirsp3_phase2.py tests/test_activity_bubbles.py tests/test_activity_drawer.py tests/test_missing_message_fix.py
```

## COMPLETENESS (mandatory — a response without it is returned unread)

- [ ] Edit 1: ctor/attr rename — grep `feedbar=` in activity_handler.py → 0
- [ ] Edit 2: `_update_status` verbatim body + `_streaming_label` plain text + `_escape_markup` DELETED
- [ ] Edit 3: progress render calls gone — grep → 0
- [ ] Edit 4: window.py lazy resolver; FeedBar construct/append/import untouched (grep = 3)
- [ ] Edit 5: 4 test files repointed + line-76 assertion change noted
- [ ] All verification commands green (paste tails)
- Related issues found, not fixed: <list or none>

Deviations carry a one-sentence rationale each.
