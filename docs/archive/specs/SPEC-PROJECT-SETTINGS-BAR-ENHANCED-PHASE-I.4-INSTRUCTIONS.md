# Implementation Phase I.4 — Window wiring + ProjectHandler + FeedHandler setter

**Spec:** `docs/specs/SPEC-SETTINGS-BAR-ENHANCED-FIX-3.md` §2.2 (window), §2.4 (project_handler), §2.3 (feed_handler setter)
**Prompt to load:** `prompts/steelFramedCodeWriter.md`
**Working dir:** `/path/to/projects/crabcakes`

## Task

This is the HIGH-RISK integration phase. Wire all the new callbacks into `ui/window.py`, add the token-guarded branch worker, add the project-close/open invalidation as NAMED METHODS (not lambdas), add the solo-target callback to `ProjectHandler`, add the deferred `set_on_auto_accept_level_changed` setter to `FeedHandler`, retire the legacy bar-update path, and apply the Round 4 build-time fix (callback ordering).

**3 files to change:**
1. `ui/window.py` — the bulk of the work
2. `ui/handlers/project_handler.py` — small additive change (solo-target callback)
3. `ui/handlers/feed_handler.py` — 1 deferred setter (from Phase I.2)

## Part A — `ui/window.py`

### A1. New `__init__` state fields

```python
self._cached_branch_by_path: dict[str, str] = {}
self._branch_request_token: int = 0
self._branch_active_token: int | None = None
self._branch_request_path: str | None = None
```

### A2. Extended `_on_feed_bar_update`

Per spec §2.2. Backward-compatible signature with keyword-only defaults. Resolves solo_target, auto_accept_level, and schedules branch refresh when needed. **Replaces** the existing 2-arg `_on_feed_bar_update` that called `_update_project_settings_from_project`.

### A3. Token-guarded branch worker

`_schedule_branch_refresh`, `_resolve_branch_worker`, `_on_branch_result`. Per spec §2.2. Captures `project_path` + `request_token` at schedule time. `_on_branch_result` checks token + path + active-project identity BEFORE writing cache (Round 3 BUG #2).

### A4. Project-close/open invalidation (NAMED METHODS — Round 3 BUG #1)

**CRITICAL:** These MUST be named `def` methods, NOT lambdas. The previous spec (FIX-2) had a `SyntaxError` because it tried to put assignments inside a tuple lambda.

- `_on_project_closed(name)` — bumps `_branch_request_token`, clears `_branch_active_token`, `_branch_request_path`. Does NOT clear `_cached_branch_by_path` (path-keyed cache persists for re-open). Registered as its OWN `set_on_project_closed` callback (handler is append-based, multi-callback).
- `_on_project_opened(name, path)` — bumps token, clears in-flight marker. **BUILD-TIME FIX (Round 4 BUG #1):** after invalidation, also calls `_on_feed_bar_update(name, ...)` to force re-evaluation so the new project gets its branch scheduled even if a previous worker was in-flight.

### A5. Callback implementations

- `_on_agent_cycle_clicked(current_solo)` — reads members, advances index, calls `set_solo_target` (which fires the callback). Per spec.
- `_on_autoaccept_cycle_clicked(current_level)` — cycles level via `FeedHandler.set_auto_accept_level`. **Does NOT optimistically rebuild the bar** (Round 3 BUG #4) — the bar updates via the `on_auto_accept_level_changed` callback after async confirmation. Preserves real member count (Round 2 BUG #3).
- `_on_solo_target_changed(project_name)` — guards active-project identity (Round 3 BUG #3); refreshes bar.
- `_on_settings_btn_clicked()` — calls `self._open_settings()` (NOT an invented `_settings_dialog`).

### A6. Wiring in `_build()`

After the existing `set_on_project_settings_update` call:
```python
self._main_content.set_on_settings_clicked(self._on_settings_btn_clicked)
self._main_content.set_on_agent_cycle(self._on_agent_cycle_clicked)
self._main_content.set_on_autoaccept_cycle(self._on_autoaccept_cycle_clicked)
self._project_handler.set_on_solo_target_changed(self._on_solo_target_changed)
self._project_handler.set_on_project_closed(self._on_project_closed)
self._project_handler.set_on_project_opened(self._on_project_opened)
self._feed_handler.set_on_auto_accept_level_changed(self._on_auto_accept_level_changed)
```

### A7. Retire legacy path

The existing `_on_feed_bar_update` at window.py:1053 currently calls `self._main_content._update_project_settings_from_project(project_name, member_count)` — which still uses `escape_for_pango` (BUG #6). **Replace** this body with the new extended `_on_feed_bar_update` (A2). The old `_update_project_settings_from_project` call site at line 1055 must be removed.

## Part B — `ui/handlers/project_handler.py`

### B1. Solo-target callback (spec §2.4)

Add to `__init__` (near `_solo_targets`):
```python
self._on_solo_target_changed: Callable[[str], None] | None = None
```

Add setter:
```python
def set_on_solo_target_changed(self, cb):
    self._on_solo_target_changed = cb
```

Modify `set_solo_target` to validate project (Round 3 BUG #3, Option A strict) and fire callback only on real change:
```python
def set_solo_target(self, project_name, member_session_key):
    if self._get_project_path(project_name) is None:
        return  # unknown project
    old = self._solo_targets.get(project_name)
    if old == member_session_key:
        return  # no change
    self._solo_targets[project_name] = member_session_key
    if self._on_solo_target_changed is not None:
        self._on_solo_target_changed(project_name)
```

### B2. Project-opened callback slot

Verify `set_on_project_opened` exists and is append-based (multi-callback). If it doesn't exist, add it (2-arg: `cb(name, path)`).

## Part C — `ui/handlers/feed_handler.py`

### C1. Deferred setter (from Phase I.2)

Add:
```python
def set_on_auto_accept_level_changed(self, cb):
    self._on_auto_accept_level_changed = cb
```

(The `_on_auto_accept_level_changed` field and `_emit_auto_accept_level_changed` helper were already added in Phase I.2.)

## Rules

- Use `steelFramedCodeWriter.md` prompt at `prompts/steelFramedCodeWriter.md`
- Read all 3 files in full before editing. Anchor to identifiers, not line numbers.
- Follow spec §2.2/§2.4/§2.3 code samples verbatim (4-round audited).
- **CRITICAL:** Every code sample must be valid Python. No assignments inside lambdas.
- **GTK thread safety:** all `_branch_*` state transitions on the GTK thread; worker only reads captured `path` and dispatches via `GLib.idle_add`.
- **No invented APIs:** `_open_settings()` exists; `get_active_project_path()` exists; `get_active_project_name()` exists; `get_project_members()` exists; `get_solo_target()` exists.

## Verification (paste output in COMPLETENESS)

1. **grep window methods:** `grep -n "def _on_feed_bar_update\|def _schedule_branch_refresh\|def _resolve_branch_worker\|def _on_branch_result\|def _on_project_closed\|def _on_project_opened\|def _on_agent_cycle_clicked\|def _on_autoaccept_cycle_clicked\|def _on_solo_target_changed\|def _on_settings_btn_clicked\|def _on_auto_accept_level_changed" ui/window.py`

2. **grep project_handler:** `grep -n "def set_on_solo_target_changed\|def set_on_project_opened\|def set_on_project_closed\|_on_solo_target_changed" ui/handlers/project_handler.py`

3. **grep feed_handler:** `grep -n "def set_on_auto_accept_level_changed" ui/handlers/feed_handler.py`

4. **grep legacy retirement:** `grep -n "_update_project_settings_from_project" ui/window.py` — confirm 0 calls (the method may still exist in main_content.py but window.py must not call it).

5. **grep forbidden:** `grep -n "Gtk.ReliefStyle\|set_relief\|for child in list\|escape_for_pango" ui/window.py` — confirm 0 matches.

6. **Import smoke test:** `python3 -c "from ui.window import MainWindow"` — no errors.

7. **Full test collection:** `python3 -m pytest --co -q` — confirm no collection regression.

## COMPLETENESS checklist (required)

```
COMPLETENESS:
- [x] A1: Added 4 __init__ state fields — evidence
- [x] A2: Extended _on_feed_bar_update (keyword-only defaults) — evidence
- [x] A3: Added _schedule_branch_refresh + _resolve_branch_worker + _on_branch_result — evidence
- [x] A4: Added _on_project_closed (named method) + _on_project_opened (named method + Round 4 fix) — evidence
- [x] A5: Added 4 callback impls — evidence
- [x] A6: Wired 7 setters in _build() — evidence
- [x] A7: Retired legacy _update_project_settings_from_project call — evidence (grep 0 calls)
- [x] B1: Added set_on_solo_target_changed + modified set_solo_target (validation + fire-on-change) — evidence
- [x] B2: Verified/added set_on_project_opened — evidence
- [x] C1: Added set_on_auto_accept_level_changed setter — evidence
- [x] grep window methods — output
- [x] grep project_handler — output
- [x] grep feed_handler — output
- [x] grep legacy retirement (0 calls) — output
- [x] grep forbidden patterns (0) — output
- [x] Import smoke test — output
- [x] Full test collection — output
```

Report back with COMPLETENESS + verification evidence. Please write when done.
