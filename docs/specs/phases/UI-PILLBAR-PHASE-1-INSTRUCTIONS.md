# UI-PILLBAR Phase 1 — Pill to the Project Bar + Opaque Bar + Gear Removal

**Request:** PM, 2026-10-02 (verified read-only before phasing — all claims confirmed).
**Builder rule:** `prompts/steelFramedCodeWriter.md` — invoke it, apply every rule.
**Touches:** `ui/styles.py`, NEW `ui/views/activity_pill.py`, `ui/views/main_content.py`,
`ui/window.py`, tests. **Does NOT touch:** `ui/views/chat_surface.py` (Phase 2),
toolbar, settings dialog paths.

---

## Verified facts (HEAD 2bb5feec)

- Bar: `main_content.py:123-130` (`_project_settings`, class `project-feed-bar`,
  28px, hidden when no project at :290-291).
- Transparency: `styles.py:33-36` `rgba(30, 30, 40, 0.75)`.
- Bar contents (rebuilt by `update_project_settings` :280-362): project+members
  label, Chat: button, Files: button, Git: label, gear (singleton :143-148,
  re-appended :361-362; click → `set_on_settings_clicked` callback → window:484 →
  `_on_settings_btn_clicked` :1329 → `_open_settings`). Toolbar ALSO has ⚙ Settings
  → same `_open_settings` (window.py:127-129). Redundant — bar gear dies.
- Pill today: per-surface `_pill_label` (chat_surface.py) + `ActivityPillAdapter`
  resolver in window.py picking the active surface; CSS `.pill-*` rules live in
  APP_CSS (GTK-side, styles.py:1650+) — they apply to ANY Gtk.Label with the class.
- `test_main_content_settings_bar.py` (14 tests) pins gear-preservation (BUG #5)
  and `set_feed_bar_text` gear re-append.

## Edit 1 — styles.py: opaque bar

`.project-feed-bar` background `rgba(30, 30, 40, 0.75)` → `#1e1e28` (same color,
alpha 1). One line.

## Edit 2 — NEW `ui/views/activity_pill.py`

Small self-contained widget (the pill relocates; its logic gets a proper home
instead of living inside chat_surface):

```python
class ActivityPillLabel(Gtk.Label):
    """Status pill for the project bar — text + state-driven CSS class.

    Owns the 7-state→class map (moved verbatim from chat_surface) and the
    swap dance (remove old, add new, stash). set_activity_status(text, state)
    is the ActivityPillAdapter contract; state None/unknown keeps the class.
    """
```

- `_ACTIVITY_STATE_TO_CSS` map moves HERE (verbatim 7 keys).
- `set_activity_status(self, text, state=None)` — set_text always; class swap
  only when state is known (mirror chat_surface's current logic).
- Constructor starts as `pill-idle` / "Idle", right-aligned, `set_margin_end(8)`.
- Module docstring notes the move (SPEC-07 SP1 placed it per-surface; PM
  relocation 2026-10-02 — one shared pill in the project bar).

## Edit 3 — main_content.py: gear out, pill in, pill-only mode

1. DELETE the gear: `self._settings_btn` construction (:143-148), the two
   re-append sites (:361-362, :431 area in `set_project_settings_text`), and
   `_on_settings_btn_clicked` (:400-402). KEEP `set_on_settings_clicked`
   (:404-405) as a no-op-compatible setter? NO — delete it and its window.py:484
   wiring (grep confirms the toolbar is the sole remaining settings path).
2. ADD in `__init__`: `self._activity_pill = ActivityPillLabel()`; append it to
   `_project_settings` (right end — after the gear's former position;
   `info_box` has `set_hexpand(True)` + START align so the pill lands right).
3. `update_project_settings` project-less branch (:290-293): instead of hiding
   the bar, show it in PILL-ONLY mode — clear children, append just the pill
   (bar stays 28px; no project info). Comment the ruling (status stays visible
   on project-less tabs).
4. `_clear_settings_bar` sibling-walk: keep (still used); the pill is re-appended
   by both rebuild paths (add to `update_project_settings` AND the pill-only
   branch). The BUG #5 gear-preservation comments die with the gear.
5. Public seam for window.py: `def activity_pill(self) -> ActivityPillLabel`
   (read accessor — the adapter resolves THIS, not a surface).

## Edit 4 — window.py: resolver repoint

The `ActivityPillAdapter(lambda: ... surface_for_key ...)` resolver becomes:

```python
status_target=ActivityPillAdapter(
    lambda: self._main_content.activity_pill()
    if getattr(self, "_main_content", None) is not None else None
),
```

(The adapter's catch-up machinery no-ops — one stable target. Keep the adapter
unchanged: it's the duck-type the handler speaks.)

## Edit 5 — tests

- NEW `tests/test_activity_pill_label.py`: map verbatim pin (7 keys), class swap
  + unknown-state keeps class, text always lands, idle defaults. Real Gtk.Label
  under the `FakeLabel` pattern from test_activity_pill_adapter.py (bare-safe).
- `tests/test_main_content_settings_bar.py`: gear tests retire WITH dispositions
  (subject dies — the gear is deleted; subject-alive grep first). `set_feed_bar_text`
  tests keep passing only if they don't assert the gear — check each; repoint the
  ones that do (list every change in the report).
- NEW pin in the settings-bar file: `update_project_settings("", 0, ...)` leaves
  the bar VISIBLE with exactly one child (the pill) — the pill-only ruling.
- `tests/test_activity_pill_adapter.py`: any test whose resolver returns a
  surface-with-set_activity_status keeps working (fakes) — verify, don't rewrite;
  the SP2-era `test_surface_state_map` imports `_ACTIVITY_STATE_TO_CSS` from
  chat_surface — repoint the import to the new module (Phase 2 removes it from
  chat_surface; moving the pin now avoids a break).

## Verification (paste full output — pyright MANDATORY, xvfb full suite)

```
xvfb-run -a .venv/bin/python -m pytest tests/test_main_content_settings_bar.py tests/test_activity_pill_label.py tests/test_activity_pill_adapter.py tests/test_chat_surface.py -v
.venv/bin/python -m pyright ui/views/main_content.py ui/views/activity_pill.py ui/window.py
python -m ruff check ui/styles.py ui/views/main_content.py ui/views/activity_pill.py ui/window.py tests/test_activity_pill_label.py
xvfb-run -a .venv/bin/python -m pytest tests/ -q --ignore=tests/test_enforcement.py --ignore=tests/test_mcp_config.py -p no:cacheprovider 2>&1 | tail -3
```

## COMPLETENESS (mandatory)

- [ ] Edit 1: opaque bar (grep rgba 0.75 gone from .project-feed-bar)
- [ ] Edit 2: ActivityPillLabel module (map moved verbatim)
- [ ] Edit 3: gear fully removed (grep _settings_btn → 0 in main_content) + pill hosted + pill-only mode + accessor
- [ ] Edit 4: resolver repointed (grep surface_for_key gone from the adapter wiring)
- [ ] Edit 5: tests — new file + dispositions table for every retired/repointed gear test + pill-only pin + map import repointed
- [ ] Full battery green incl. pyright + full suite
- Related issues found, not fixed: <list or none>

Deviations carry a one-sentence rationale each.
