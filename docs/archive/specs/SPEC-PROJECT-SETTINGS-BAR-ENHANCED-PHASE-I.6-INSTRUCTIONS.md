# Implementation Phase I.6 — ARCHITECTURE.md update

**Spec:** `docs/specs/SPEC-PROJECT-SETTINGS-BAR-ENHANCED-FIX-3.md` §8
**Prompt to load:** `prompts/steelFramedCodeWriter.md`
**Working dir:** `/path/to/projects/crabcakes`

## Task

Update `docs/ARCHITECTURE.md` to document the new public APIs and wiring added by this feature. Per the spec §8, 4 updates are required. Per the project convention (ARCHITECTURE.md is the law — must be updated in the same commit as structural code changes).

## File to change

**`docs/ARCHITECTURE.md`** — only this file.

## Updates required (per spec §8)

### 1. `ui/handlers/feed_handler.py` public API (§3.16 or equivalent)

Document:
- `get_auto_accept_level() -> str` — returns "off"|"diffs"|"files"|"all" (file-change scoped; exec is separate axis)
- `set_auto_accept_level(level: str) -> None` — routes enabling through warning gate; commits via `_refresh_auto_accept_state`
- `set_on_auto_accept_level_changed(cb) -> None` — callback fired after commit (post-confirmation for async warning path)

Note the file-scoped semantics: the bar label is "⚡ files: ..." and exec_command auto-accept is intentionally NOT collapsed into this label.

### 2. `ui/handlers/project_handler.py` public API

Document:
- `set_on_solo_target_changed(cb)` — callback fired after `set_solo_target` changes (real change only, validated project only)

### 3. `ui/views/main_content.py` module-responsibility section

**IMPORTANT (Round 1 BUG #14):** The original spec cited §3.7, but §3.7 documents `ui/views/left_panel.py`, NOT `main_content.py`. Find the ACTUAL section that documents `main_content.py` (it may be a different §3.x or a general views section) and add:
- `update_project_settings(project_name, member_count, solo_target, auto_accept_level, branch_name)` — rebuilds the settings bar with all 5 elements
- `set_on_settings_clicked(cb)`, `set_on_agent_cycle(cb)`, `set_on_autoaccept_cycle(cb)` — 3 new callback setters
- `_clear_settings_bar()` — sibling-walk child removal (replaces the broken `list(box)` pattern)
- Gear-preservation invariant: both legacy methods (`set_project_settings_text`, `set_feed_bar_text`) re-append `_settings_btn` after clearing

### 4. `ui/window.py` §3.6 wiring note

Document the new settings-bar callbacks wired in `_build()`:
- `set_on_settings_clicked` → `_on_settings_btn_clicked` → `_open_settings()`
- `set_on_agent_cycle` → `_on_agent_cycle_clicked` → member cycle + `set_solo_target`
- `set_on_autoaccept_cycle` → `_on_autoaccept_cycle_clicked` → `FeedHandler.set_auto_accept_level` (no optimistic rebuild)
- `set_on_solo_target_changed` → `_on_solo_target_changed` → bar refresh (active-project guarded)
- `set_on_project_opened` / `set_on_project_closed` → named invalidation methods (token + cache lifecycle)
- `set_on_auto_accept_level_changed` → `_on_auto_accept_level_changed` → bar refresh after async confirmation

Also document the token-guarded branch worker:
- `_schedule_branch_refresh` / `_resolve_branch_worker` / `_on_branch_result` — async branch resolution with monotonic token + path-keyed cache
- State fields: `_cached_branch_by_path`, `_branch_request_token`, `_branch_active_token`, `_branch_request_path`

## Rules

- Use `steelFramedCodeWriter.md` prompt
- Read `docs/ARCHITECTURE.md` in full before editing. Find the actual section numbers (don't trust the spec's citations — Round 1 BUG #14 showed they were wrong).
- Anchor to section headings, not line numbers.
- Keep entries concise — match the existing ARCHITECTURE.md style (one-line descriptions, not essays).
- Do NOT restructure existing sections — only add/update entries within existing sections.

## Verification (paste output in COMPLETENESS)

1. **grep:** `grep -n "get_auto_accept_level\|set_auto_accept_level\|set_on_auto_accept_level_changed\|set_on_solo_target_changed\|update_project_settings\|set_on_agent_cycle\|set_on_autoaccept_cycle\|_clear_settings_bar\|set_on_settings_clicked" docs/ARCHITECTURE.md` — confirm the new APIs appear.
2. **grep (branch worker):** `grep -n "_schedule_branch_refresh\|_cached_branch_by_path\|_branch_active_token" docs/ARCHITECTURE.md` — confirm documented.
3. **No stale §3.7 citation:** verify the MainContent API is NOT documented under a section about `left_panel.py`.

## COMPLETENESS checklist (required)

```
COMPLETENESS:
- [x] Update 1: FeedHandler public API (3 methods) — evidence
- [x] Update 2: ProjectHandler set_on_solo_target_changed — evidence
- [x] Update 3: MainContent API (update_project_settings + 3 setters + clear helper + gear invariant) — evidence (correct section, NOT §3.7)
- [x] Update 4: Window wiring note (7 callbacks + branch worker + 4 state fields) — evidence
- [x] grep confirms new APIs appear — output
- [x] grep confirms branch worker documented — output
- [x] No stale §3.7 citation — evidence
```

Report back with COMPLETENESS + verification evidence. Please write when done.
