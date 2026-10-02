# UI-PILLBAR Phase 2 — Surface-Pill Retirement + Dead-Code Sweep

**Depends on:** Phase 1 (bab9d1cb).
**Builder rule:** `prompts/steelFramedCodeWriter.md` — invoke it, apply every rule.
**Touches:** `ui/views/chat_surface.py`, `ui/styles.py`, `ui/views/main_content.py`,
tests. **Does NOT touch:** activity_pill.py, window.py, toolbar.

---

## Edit 1 — chat_surface: per-surface pill retires

Delete from BOTH `ChatSurface` and `TextViewFallback` (subject: the shared bar pill
is the live one; the surface pill is static "Idle" since P1 — auditor-verified):

- `_pill_label` construction + append (both `__init__`s)
- `set_activity_pill` (both classes) + `_PILL_STATES` module dict
- `set_activity_status` (both classes)
- The module docstring's pill mentions
- The API-parity comment block explaining why both classes mirror pill methods

**Grep gates (zero after):** `_pill_label`, `set_activity_pill`, `set_activity_status`,
`_PILL_STATES` in chat_surface.py.

## Edit 2 — styles.py: dead CSS out

Delete `.project-bar-gear` + `:hover/:active` rules (:80-91 area — zero widget users,
auditor-verified). The `.pill-*` rules STAY (the bar pill uses them).

## Edit 3 — main_content.py: dead methods out

Delete `_update_project_settings_from_project` (:477 area — zero callers). For
`set_project_settings_text` / `set_feed_bar_text` (caller-less in production,
auditor-verified): **keep both** — they host the preservation-contract tests
(pill-preserved-through-rebuilds) that guard `_clear_settings_bar`'s sibling-walk.
Add a one-line comment on each: "No production caller; test-only preservation
contract (auditor note, P2). Delete with its tests when the bar rebuild unifies."

## Edit 4 — tests

- `tests/test_chat_surface.py`: pill tests retire WITH dispositions (subject-alive
  grep first: `git show bab9d1cb:ui/views/chat_surface.py | grep set_activity`
  proves alive pre-P2, dead after). List each retired test + name.
- `tests/test_activity_pill_adapter.py`: the SP1-era tests that drive fake
  surfaces with `set_activity_status` still pass (fakes carry the method — the
  adapter contract is target-agnostic). VERIFY green; if any test imports
  `_ACTIVITY_STATE_TO_CSS` from chat_surface (repointed in P1 — confirm) nothing
  more needed.
- NEW pin in `tests/test_activity_pill_label.py` (recorder layer, bare-safe):
  `test_bar_pill_is_sole_pill` — grep-style source pin? NO — runtime pin: import
  chat_surface, assert `not hasattr(ChatSurface, "set_activity_pill")` and
  `not hasattr(ChatSurface, "set_activity_status")` (the retirement contract;
  a regression re-adding them fails this).

## Verification (paste full output — pyright MANDATORY, xvfb full suite)

```
grep -n "_pill_label\|set_activity_pill\|set_activity_status\|_PILL_STATES" ui/views/chat_surface.py   # ZERO
grep -n "project-bar-gear" ui/styles.py ui/                                                             # ZERO
xvfb-run -a .venv/bin/python -m pytest tests/test_chat_surface.py tests/test_activity_pill_label.py tests/test_activity_pill_adapter.py tests/test_main_content_settings_bar.py -v
.venv/bin/python -m pyright ui/views/chat_surface.py ui/views/main_content.py ui/styles.py
python -m ruff check ui/views/chat_surface.py ui/styles.py ui/views/main_content.py
xvfb-run -a .venv/bin/python -m pytest tests/ -q --ignore=tests/test_enforcement.py --ignore=tests/test_mcp_config.py -p no:cacheprovider 2>&1 | tail -3
```

## COMPLETENESS (mandatory)

- [ ] Edit 1: both classes' pill code gone — grep ZERO (4 patterns)
- [ ] Edit 2: gear CSS gone — grep ZERO
- [ ] Edit 3: _update_project_settings_from_project gone; two setters kept + commented
- [ ] Edit 4: chat_surface pill tests retired + disposition table; retirement-contract pin added
- [ ] Full battery green incl. pyright + full suite
- Related issues found, not fixed: <list or none>

Deviations carry a one-sentence rationale each.
