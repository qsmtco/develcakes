# SPEC-07 SP1 — Activity-Pill Adapter + Surface Seam (R4 feedbar removal, 1 of 3)

**Spec:** `docs/specs/SPEC-07-R4-FEEDBAR-REMOVAL.md` (read §2 AMENDED — it supersedes
the original sketch). Architecture: `.crabcakes/architecture.md` §Chat surface.
**Builder rule:** `prompts/steelFramedCodeWriter.md` — invoke it, apply every rule.
**This phase touches:** `ui/views/chat_surface.py`, `ui/handlers/chat_render_handler.py`,
one NEW test file. It does NOT touch activity_handler.py, window.py, or feedbar.py.

---

## What you are building

FeedBar is being deleted (SP3). Its job — surfacing the 6-state activity machine —
moves to the per-session chat surface's activity pill. Today the pill
(`set_activity_pill`, 4 states) has ZERO production callers. SP1 builds the seam
everything else will hang on; nothing existing changes behavior.

Verified facts you can rely on (HEAD 3972ac9e):

- `ui/views/chat_surface.py` — two classes, `ChatSurface` (WebKit) and
  `TextViewFallback` (alias `ChatSurface = TextViewFallback` when WebKit is absent).
  Both have `self._pill_label` (Gtk.Label), `self._pill_css` (str), and
  `set_activity_pill(state)`. `_PILL_STATES` dict at module level has exactly 4 keys:
  idle, thinking, tool, error. `_BASE_CSS` has `.pill-idle/.pill-thinking/.pill-tool/.pill-error`.
- `ui/handlers/chat_render_handler.py` — `self._surfaces: dict[str, ChatSurface]`
  (session_key → surface). `_surface_for(session_key, mount_key=None)` CREATES and
  MOUNTS on miss — do not use it for read-only lookup. `surface_for_box(chat_box)`
  exists (O(1) index) but takes a box, not a key.

## Edit 1 — chat_surface.py: 6→pill-class state map + new CSS

Add a module-level map (below `_PILL_STATES`):

```python
# SPEC-07 SP1: activity-machine state → pill CSS class (6 handler states +
# error → the 4-class pill vocabulary, extended with streaming/done).
_ACTIVITY_STATE_TO_CSS = {
    "idle": "pill-idle",
    "sending": "pill-thinking",
    "reasoning": "pill-thinking",
    "streaming": "pill-streaming",
    "tool_use": "pill-tool",
    "done": "pill-done",
    "error": "pill-error",
}
```

Append to `_BASE_CSS` (same style as the existing pill rules):

```
.pill-streaming { color: #7dcfff; }
.pill-done { color: #9ece6a; }
```

## Edit 2 — chat_surface.py: `set_activity_status` on BOTH classes

Add to `ChatSurface` (and mirror in `TextViewFallback` — both classes must stay
API-identical; the module-level alias depends on it):

```python
def set_activity_status(self, text: str, state: str | None = None) -> None:
    """SPEC-07 SP1: activity-machine status → pill text + CSS class.

    text always lands on the pill label (plain text — callers must NOT send
    Pango markup; feedbar markup dies with feedbar). state (one of the
    ActivityHandler 6 + error) drives the CSS class; None keeps the current
    class. Unknown state → keep current class (fail-quiet, not crash).
    """
```

Body: `self._pill_label.set_text(text)` always; if `state` is in
`_ACTIVITY_STATE_TO_CSS`, swap the CSS class exactly the way
`set_activity_pill` does (remove old, add new, update `self._pill_css`).
Do not refactor `set_activity_pill` — leave it as-is.

## Edit 3 — chat_render_handler.py: `surface_for_key`

Add next to `surface_for_box`:

```python
def surface_for_key(self, session_key: str):
    """SPEC-07 SP1: READ-ONLY surface lookup by session key (or None).

    Deliberately NOT _surface_for() — that method creates and mounts on a
    miss; status resolution runs on a 250ms tick and must be side-effect
    free. A miss here simply means "no surface yet" → caller renders nothing.
    """
    return self._surfaces.get(session_key)
```

## Edit 4 — chat_surface.py: `ActivityPillAdapter`

Module-level class (after `TextViewFallback`, before the alias block —
the adapter is GTK-free logic + surface calls, works under both surfaces):

```python
class ActivityPillAdapter:
    """SPEC-07 SP1: FeedBar duck-type → the ACTIVE chat surface's pill.

    The ActivityHandler calls five methods today (verified at HEAD):
    set_status_text, set_progress_fraction, set_progress_hidden,
    set_progress_pulse, pulse_progress. The pill has no progress element,
    so the progress quartet are documented no-ops. set_status_text carries
    (text, state); the adapter re-applies the last status when the resolver
    returns a NEW surface (tab switch / lazy-create catch-up).
    """
```

Contract:
- `__init__(self, resolver)`: resolver is `callable() -> surface | None`.
- Caches `self._last_surface`, `self._last_text`, `self._last_state`.
- `set_status_text(self, text, state=None)`: resolve surface; if None → update
  cache and return (no-op). If surface identity differs from `_last_surface`
  AND `_last_text is not None` → apply the CACHED text/state first (a fresh
  surface starts "Idle"; catch-up), then apply the new (text, state) — net
  effect: new surface shows the new status. If same surface → apply directly.
  Always update the cache.
- `set_progress_fraction(f)` / `set_progress_hidden(b)` / `set_progress_pulse(e)` /
  `pulse_progress()`: no-ops, one-line each, with a `# no-op: pill has no
  progress element (SPEC-07 §2 AMENDED)` comment.
- No GTK imports needed; no exceptions raised on weird input (resolver throwing
  → catch and treat as None? NO — let it propagate; the resolver is our own
  lambda in window.py and must not throw. Document that in the docstring.)

## Edit 5 — NEW test file `tests/test_activity_pill_adapter.py`

Red-first: write the tests BEFORE the implementation, prove they fail (run and
paste the red output), then implement, then paste the green output. Tests must
NOT import WebKit (use a fake surface object with `set_activity_status` recorded,
plus one real-`TextViewFallback`-shaped test only if cheap — prefer pure fakes;
follow the no-gi pattern used by tests/test_welcome_bubble.py if you need a
widget, but fakes are preferred for adapter logic).

Required coverage (each named test):

1. `test_status_text_lands_on_resolved_surface` — resolver returns fake; adapter
   `.set_status_text("Working", "reasoning")` → fake recorded ("Working", "reasoning").
2. `test_state_none_keeps_css_untouched` — surface records; state=None call passes
   None through to the surface (surface decides); assert call shape.
3. `test_none_surface_is_silent_noop` — resolver returns None; `.set_status_text(...)`
   must not raise; cache still updated so a later surface gets the latest.
4. `test_new_surface_gets_cached_status_catchup` — surface A gets status X;
   resolver now returns fresh surface B; adapter `.set_status_text(Y, s2)` →
   B must receive the catch-up apply of X first, then (Y, s2). Assert call order.
5. `test_progress_quartet_are_noops` — each of the four progress methods on a
   resolved surface: zero calls to the surface, no exceptions.
6. `test_surface_state_map` (real `_ACTIVITY_STATE_TO_CSS` import) — all 7 keys
   map into the pill CSS vocabulary; streaming→pill-streaming, done→pill-done
   present; idle→pill-idle.
7. `test_set_activity_status_unknown_state_keeps_class` — fake label-like object
   or TextViewFallback under xvfb marker if a real widget is needed; if you use
   a pure fake for the label the test needs no xvfb — prefer that.
8. `test_surface_for_key_readonly` — construct a real ChatRenderHandler the way
   tests/test_chat_render_handler.py does (copy its fixture pattern), assert:
   miss → None (and NO surface created — len(handler._surfaces) unchanged);
   after a real render creates one, hit → the surface object.

Also add ONE pin that the handler still compiles against the future contract
(write it now, it stays green through SP2):
9. `test_adapter_satisfies_handler_call_surface` — inspect.signature check that
   the adapter has all five method names the handler calls at
   activity_handler.py:683-871 (set_status_text, set_progress_fraction,
   set_progress_hidden, set_progress_pulse, pulse_progress).

## Verification commands (paste full output in your report)

```
python -m pytest tests/test_activity_pill_adapter.py -v
python -m pytest tests/test_chat_surface.py tests/test_chat_render_handler.py -v
python -m ruff check ui/views/chat_surface.py ui/handlers/chat_render_handler.py tests/test_activity_pill_adapter.py
```

All three green + ruff clean. The two existing suites are your regression gate —
`set_activity_pill` behavior must be byte-identical (you didn't touch it).

## Greps for your report

```
grep -n "set_activity_status\|ActivityPillAdapter\|surface_for_key\|_ACTIVITY_STATE_TO_CSS" ui/views/chat_surface.py ui/handlers/chat_render_handler.py
```

## Report format

COMPLETENESS checklist (mandatory — a response without it is returned unread):
- [ ] Edit 1: state map + CSS — evidence (grep lines)
- [ ] Edit 2: set_activity_status ×2 classes — evidence
- [ ] Edit 3: surface_for_key — evidence
- [ ] Edit 4: ActivityPillAdapter — evidence
- [ ] Edit 5: test file, 9+ tests — RED output pasted first, then GREEN
- [ ] Regression suites green (paste tail)
- [ ] Ruff clean (paste)
- Related issues found, not fixed: <list or none>

Deviations from these instructions must carry a one-sentence rationale each.
