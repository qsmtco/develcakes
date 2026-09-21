# Implementation Phase I.3 — MainContent settings bar widget refactor

**Spec:** `docs/specs/SPEC-PROJECT-SETTINGS-BAR-ENHANCED-FIX-3.md` §2.1
**Prompt to load:** `prompts/steelFramedCodeWriter.md`
**Working dir:** `/path/to/projects/crabcakes`

## Task

Refactor the settings bar in `ui/views/main_content.py` to support the new multi-element layout. This is the biggest single-file phase: new bar rebuild method, 4 click handlers, 3 setters, child-clear helper, name-resolution helper, gear button init, and fixes to the two legacy methods.

## File to change

**`ui/views/main_content.py`** — only this file.

## What to add (per spec §2.1, all verified across 4 audit rounds)

1. **`_clear_settings_bar()`** — sibling-walk child removal (replaces the broken `for child in list(box): box.remove(child)` pattern). Uses `get_first_child()` / `remove()`.

2. **`update_project_settings(project_name, member_count, solo_target, auto_accept_level, branch_name)`** — the main bar rebuild. Hides bar on empty project. Uses `xml_escape_text` for project name and branch (NOT `escape_for_pango` — Round 2 BUG #6). Agent/auto labels are `Gtk.Button` labels (plain text, safe). Appends info_box + settings_btn.

3. **`_resolve_agent_display_name(session_key)`** — ordered fallback: `_agent_mgr.get_name(sk)` → `ARTH.get_special_agents().get(sk)` (with truthiness check, Round 3 BUG #7) → session_key as-is.

4. **3 click handlers:** `_on_agent_label_clicked(current_solo)`, `_on_autoaccept_label_clicked(current_level)`, `_on_settings_btn_clicked(_btn)`.

5. **3 setters:** `set_on_settings_clicked(cb)`, `set_on_agent_cycle(cb)`, `set_on_autoaccept_cycle(cb)`.

6. **`__init__` additions** (after `self._project_settings` construction): `_settings_btn` (Gtk.Button with `set_has_frame(False)`, `set_focus_on_click(False)`, CSS class `project-bar-gear`), plus 3 callback fields initialized to None.

7. **Fix legacy `set_project_settings_text` and `set_feed_bar_text`** — replace `for child in list(...)` with `_clear_settings_bar()`, AND re-append `self._settings_btn` after the label (Round 2 BUG #5).

## Rules

- Use `steelFramedCodeWriter.md` prompt at `prompts/steelFramedCodeWriter.md`
- Read `ui/views/main_content.py` in full before editing. Anchor to method/identifier names, not line numbers.
- Follow spec §2.1 code samples verbatim.
- **GTK4 invariants:** `set_has_frame(False)` (NOT `set_relief`/`Gtk.ReliefStyle` — removed in GTK4). `set_focus_on_click(False)`.
- **Pango invariants:** `xml_escape_text()` for project name and branch (untrusted plain text interpolated into markup). `Gtk.Button` labels are plain text — no escaping needed for agent/auto labels.
- **Child-clear invariant:** use the sibling-walk `while get_first_child() is not None: remove(get_first_child())` pattern. NEVER `for child in list(box)`.
- **Gear-preservation invariant:** both legacy methods must re-append `self._settings_btn` after `_clear_settings_bar()`.

## Verification (paste output in COMPLETENESS)

1. **grep:** `grep -n "def update_project_settings\|def _clear_settings_bar\|def _resolve_agent_display_name\|def _on_agent_label_clicked\|def _on_autoaccept_label_clicked\|def _on_settings_btn_clicked\|def set_on_settings_clicked\|def set_on_agent_cycle\|def set_on_autoaccept_cycle" ui/views/main_content.py` — confirm 9 new definitions.

2. **grep (forbidden patterns):** `grep -n "Gtk.ReliefStyle\|set_relief\|for child in list" ui/views/main_content.py` — confirm 0 matches in the new code.

3. **grep (escape):** `grep -n "escape_for_pango\|xml_escape_text" ui/views/main_content.py` — confirm `xml_escape_text` used for project name + branch; no `escape_for_pango` on untrusted plain text.

4. **Import smoke test:** `python3 -c "from ui.views.main_content import MainContent"` — no errors (may segfault on widget construction in sandbox — that's environmental, note it).

5. **Gear-preservation test:** construct or mock MainContent; call `set_project_settings_text("x")`; assert `self._settings_btn` still has a parent.

## COMPLETENESS checklist (required)

```
COMPLETENESS:
- [x] Edit 1: Added _clear_settings_bar() — evidence
- [x] Edit 2: Added update_project_settings() — evidence
- [x] Edit 3: Added _resolve_agent_display_name() (with .get() + truthiness) — evidence
- [x] Edit 4: Added 3 click handlers — evidence
- [x] Edit 5: Added 3 setters — evidence
- [x] Edit 6: Added _settings_btn init in __init__ — evidence
- [x] Edit 7: Fixed set_project_settings_text (sibling-walk + gear re-append) — evidence
- [x] Edit 8: Fixed set_feed_bar_text (sibling-walk + gear re-append) — evidence
- [x] grep confirms 9 new definitions — output
- [x] grep confirms 0 forbidden patterns (ReliefStyle/set_relief/list-box) — output
- [x] grep confirms xml_escape_text for name/branch — output
- [x] Import smoke test — output (note segfault if environmental)
- [x] Gear-preservation test — output
```

Report back with COMPLETENESS + verification evidence. Please write when done.
