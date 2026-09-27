# SPEC-07 SP3 — FeedBar Deletion + Retirement Sweep (R4 feedbar removal, 3 of 3)

**Spec:** `docs/specs/SPEC-07-R4-FEEDBAR-REMOVAL.md` (read §2 AMENDED + §5).
**Builder rule:** `prompts/steelFramedCodeWriter.md` — invoke it, apply every rule.
**Depends on:** SP1 + SP2 landed and committed.
**Touches:** deletions + `ui/window.py` + `tests/test_uirsp3_phase2.py` + ARCHITECTURE.md.
**Does NOT touch:** activity_handler.py, chat_surface.py, chat_render_handler.py.

---

## Goal

Delete the dead widget, unwind the window wiring, retire its tests WITH
DISPOSITIONS (the subject died — verify with the subject-alive grep BEFORE
retiring any guard/test; the standing rule from SPEC-06 post-mortem).

## Edit 1 — delete `ui/views/feedbar.py`

`rm ui/views/feedbar.py`. The duck-type lives in the adapter (SP1); the handler is
repointed (SP2). Nothing imports it except window.py (:36, dies now) and the
uirsp3 widget tests (die in Edit 4).

## Edit 2 — window.py unwire (identifier-anchored)

- Delete import :36 `from ui.views.feedbar import FeedBar`.
- Delete :255-256 comment + `self._response_status = FeedBar()`.
- Delete `right_box.append(self._response_status)` :773 and fix the :771 comment
  ("feedbar above main content" → describes right_box = main content only).
- Gate: `grep -n "FeedBar\|feedbar\|_response_status" ui/window.py` → ZERO.
- SETTINGS-BAR GUARD: `grep -n "feed_bar" ui/window.py` may still show
  `_on_feed_bar_update` — that is the PROJECT SETTINGS BAR (different widget,
  explicitly out of scope per spec §OUT OF SCOPE). Do not touch it.

## Edit 3 — chat_handler.py:420 comment fix (1 line)

`# Trigger Pre Flight state in ActivityHandler (FeedBar status bar)` →
`# Trigger Pre Flight state in ActivityHandler (activity pill, SPEC-07 R4)`.

## Edit 4 — retire test_uirsp3_phase2.py FeedBar-widget tests WITH DISPOSITIONS

The file mixes three concerns. Handle each:

a) **Handler rows 1-3 + audit follow-ups + F12 tests** (use the MagicMock target):
   already repointed in SP2 — UNTOUCHED here.
b) **FeedBar widget tests** (rows 4: `test_hidden_progress_bar_not_visible`,
   `test_set_progress_opacity_zero_hides_bar`, and the two `set_status_text`
   markup-dedupe tests `test_set_status_text_identical_markup_renders_once` /
   `test_set_status_text_changed_markup_renders_again` /
   `test_set_status_text_dedupe_is_exact_string_not_prefix`): SUBJECT DIED
   (feedbar.py deleted). Remove them and replace the import block + module
   docstring lines that reference FeedBar. Keep the file's slow/manual row.
c) Replace the module docstring's FeedBar references with the pill status_target.

Disposition table (report this):

| Retired test | Disposition | Verified by |
|---|---|---|
| test_hidden_progress_bar_not_visible | subject died (feedbar deleted) | `ls ui/views/feedbar.py` fails |
| test_set_progress_opacity_zero_hides_bar | subject died | same |
| 3× set_status_text markup-dedupe tests | subject died (markup cache lived in FeedBar; pill has no markup path) | same + SP1 pill tests cover class swap |

The markup-dedupe INVARIANT itself (identical text shouldn't re-layout) is NOT
re-implemented in the pill: Gtk.Label.set_text already dedupes identical text
(GTK4 skips layout when text is unchanged — a widget-level behavior, not ours).
Document that in the disposition, do not re-pin it.

## Edit 5 — scrub historical "feedbar" mentions from SP1 artifacts

SP1 landed explanatory docstrings/comments that name feedbar — they trip this
phase's grep gates. Scrub (keep meaning, drop the word):
- `ui/views/chat_surface.py` — `ActivityPillAdapter` class docstring "FeedBar
  duck-type" → "old status-bar duck-type"; `set_activity_status` docstring
  "feedbar markup dies with feedbar" → "the old status bar's markup is gone".
- `tests/test_activity_pill_adapter.py` — header "(R4 feedbar removal)" →
  "(R4 status-bar removal)"; any other in-file feedbar mentions likewise.
Gate: `grep -rn "feedbar\|FeedBar" ui/views/chat_surface.py tests/test_activity_pill_adapter.py` → ZERO.

## Edit 6 — ARCHITECTURE.md note

§Modules / Chat surface: append one sentence — "The activity pill (SPEC-07 R4) is
the activity surface; FeedBar is deleted; ActivityHandler renders via the
status_target duck-type (ActivityPillAdapter)."

## Verification commands (paste full output)

```
grep -rn "FeedBar\|feedbar" --include="*.py" ui/ agent/ transport/ render/ main.py utils/   # ZERO
grep -rn "FeedBar\|feedbar\|_response_status" tests/                                        # ZERO
grep -n "feed_bar" ui/window.py ui/views/main_content.py                                    # settings bar only — untouched
python -m pytest tests/test_uirsp3_phase2.py tests/test_activity_bubbles.py tests/test_activity_drawer.py tests/test_missing_message_fix.py tests/test_activity_pill_adapter.py -v
python -m pytest tests/ -q --ignore=tests/test_enforcement.py --ignore=tests/test_mcp_config.py -p no:cacheprovider 2>&1 | tail -5
python -m ruff check ui/ tests/
python -m pyright ui/window.py 2>&1 | tail -3
```

## COMPLETENESS (mandatory)

- [ ] Edit 1: feedbar.py deleted (ls fails)
- [ ] Edit 2: window.py zero FeedBar refs; settings bar untouched (grep pasted)
- [ ] Edit 3: chat_handler comment fixed
- [ ] Edit 4: widget tests retired + disposition table pasted
- [ ] Edit 5: SP1 docstring/comment scrub — grep pasted (zero)
- [ ] Edit 6: ARCHITECTURE.md sentence added
- [ ] All verification commands green (paste tails)
- Related issues found, not fixed: <list or none>
