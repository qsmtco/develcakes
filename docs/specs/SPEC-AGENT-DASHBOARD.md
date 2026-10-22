# SPEC: Agent dashboard in the activity drawer

**Date:** 2026-10-22
**Author:** Micah (handoff spec)
**Status:** Draft — for implementation
**Implements:** none
**Depends on:** SPEC-WIRE-CONTEXT-PRESSURE (breakdown `usage_percent` is the request size)
**Target branch:** main

> Architecture: the new view is a `Gtk.Box` in `ui/views/`. It does not import `agent/` or call compact/clear. `window.py` owns the button actions, the confirm dialog, and the row data. `AgentRuntimeHandler.compact_conversation` and `clear_conversation` stay the only mutators. `ProjectHandler.get_project_members` stays the membership list. The feed and the project-bar state machine are not touched.

DISCOVERY:
- Read `ui/views/activity_drawer.py`: `ActivityDrawer` is a vertical `Gtk.Box`. Header has a toggle button whose label is `"▶ Activity"` collapsed and `"▼ Activity"` expanded (`_apply_expanded_state`). The list is a `Gtk.ScrolledWindow` (`set_max_content_height(200)`, `set_propagate_natural_height(True)`) hidden while collapsed. `toggle()` flips `_expanded`. `clear_events()` wipes rows. The widget is a view: no agent imports.
- Read `ui/window.py`: the drawer is constructed near line 854, handed to `ActivityWiringHandler`, and `wire()` is called. At the end of `_build`, `main_content` is the start child of a vertical `Gtk.Paned` and the drawer is the end child (`set_position(600)`, `set_shrink_end_child(True)`, `set_resize_end_child(False)`). Project open calls `self._activity_drawer.clear_events()`. `set_on_members_changed` is already `lambda n, m: self._on_feed_bar_update(n, len(m))`. `set_on_agent_start` / `set_on_agent_end` already update the activity pill. Stop-all confirm is a `Gtk.MessageDialog` (`transient_for=self`, `modal=True`, `message_type=WARNING`, `buttons=NONE`, `add_button`, `set_default_response(CANCEL)`, a `_dispatched` guard, `connect("response")`, `close()`, `present()`).
- Read `ui/handlers/activity_wiring_handler.py`: `wire()` sets command-output, activity-bubble, and drawer-lifecycle callbacks that call `self._drawer.append_event` and `on_agent_start`. If the drawer is not mounted, those callbacks must not run.
- Read `ui/agent_runtime/session.py`: `clear_conversation(session_key) -> bool`. Returns `False` when the key is not `agent:…`, the agent is unregistered, runtime lookup fails, or `rt.is_loop_active(session_key)` is true (it logs and does not wipe). `compact_conversation(session_key, focus_text="", *, protect_open_turn=False) -> dict` with `messages_removed`, `tokens_freed`, `summary_chars`, `layer`. On refusal it returns those ints as 0 and does not raise. `protect_open_turn=True` keeps the open turn. Textual path calls `rt.force_compact(conv, target_budget, preserve_current_turn=protect_open_turn)`.
- Read `ui/handlers/agent_runtime_handler.py`: `get_special_agents() -> dict[str, str]` is `{session_key: display_name}`. `get_special_agent_def(session_key)` returns the def or `None` (`role` is the color key). `get_last_breakdown(session_key) -> dict | None`. `runtime_for(session_key)` returns the runtime or `None`. `set_on_token_breakdown_extra` is one slot, already used by the button-bar meter.
- Read `agent/runtime.py`: `is_loop_active(session_key) -> bool` under `self._lock`.
- Read `ui/handlers/project_handler.py`: `get_project_members(project_name) -> list[str]`. `get_active_project_name() -> str | None`. `toggle_agent` calls `_on_members_changed(name, members)` when set.
- Read `models/colors.py`: `color_for_special_agent(role: str) -> str`. Empty role returns `"#6366f1"`.
- Read `ui/views/main_content.py`: `set_context_meter` is the single button-bar meter. A negative percent sets fraction 0 and returns before the percent label is written. This spec does not change that widget.
- Architecture owner: drawer shell position — `window.py` paned. Row widgets — new view. Context mutation — `AgentRuntimeHandler`. Membership — `ProjectHandler`.
- Existing pattern to copy: ActivityDrawer’s collapsed header plus scrolled body. Stop-all’s `MessageDialog` for Clear. `compact_conversation` / `clear_conversation` as they stand. Do not add a second compact implementation.

---

## 1. Overview

The activity drawer under the chat lists events the project feed and the project-bar state machine already show. The shell stays: a header with a disclosure triangle, collapsed by default, and a panel that opens under the chat. The panel becomes an agent dashboard.

Each local agent in the open project is one row: name, context meter, working or idle, Compact, Clear. The meter is that agent’s last `usage_percent` (the request size from SPEC-WIRE-CONTEXT-PRESSURE). Compact is one click. Clear asks first. Clear is not available while that agent’s loop is running. The collapsed header still shows each agent’s percent, so the drawer does not have to be open to see who is full.

### Scope

| In | Out |
|---|---|
| New dashboard view in the existing paned slot | Deleting `activity_drawer.py` or its tests |
| Rows for local project members | Gateway agents, feed cards, tool-event rows |
| Compact and confirmed Clear | A new compaction algorithm |
| Header summary while collapsed | Changing the button-bar meter beside Send |
| Refresh on breakdown, turn start/end, project open, membership change | A timer poll |

### Principles

- The view paints rows. It does not call `compact_conversation` or `clear_conversation`.
- One agent, one row, keyed by `agent:<slug>`.
- A missing breakdown is an em dash, not `0%`. `0%` means a real report of an empty request.
- Clear while a loop is active does not call `clear_conversation`. The button is insensitive.
- Compact while a loop is active calls `compact_conversation(..., protect_open_turn=True)`.
- After either action, the cached breakdown for that key is dropped. The row shows an em dash until the next model call. The old percent is the pre-action request and would lie.

---

## 2. Changes by File

### 2.1 `ui/agent_dashboard_model.py` (new)

No GTK. No `agent/` import.

```python
def local_member_keys(members: list[str], registered: dict[str, str]) -> list[str]:
    """Members that are registered local agents, in `members` order."""
    return [sk for sk in members if isinstance(sk, str) and sk in registered]


def collapsed_summary(rows: list[dict]) -> str:
    """One line for the drawer header.

    Each row needs `name` (str) and `usage_percent` (float or None).
    A missing or negative percent renders as an em dash.
    """
    if not rows:
        return "No agents"
    parts: list[str] = []
    for row in rows:
        name = row.get("name") or "Agent"
        pct = row.get("usage_percent")
        if isinstance(pct, bool) or not isinstance(pct, (int, float)):
            parts.append(f"{name} —")
        elif pct != pct or pct < 0:  # NaN
            parts.append(f"{name} —")
        else:
            parts.append(f"{name} {pct:.0f}%")
    return " · ".join(parts)
```

`bool` is excluded because `bool` is a subclass of `int`.

Line count: about +40.

### 2.2 `ui/views/agent_dashboard.py` (new)

`class AgentDashboard(Gtk.Box)`. Vertical box, CSS class `agent-dashboard`.

Constructor builds:

- Header `Gtk.Box`, CSS `agent-dashboard-header`.
- Toggle `Gtk.Button`. Click calls `toggle()`.
- Summary `Gtk.Label`, xalign 0, hexpand, ellipsize end.
- `Gtk.ScrolledWindow`, same policy and `set_max_content_height(200)` / `set_propagate_natural_height(True)` as `ActivityDrawer._build_list`. Hidden while collapsed.
- Inner `Gtk.Box` vertical, not a `ListBox`. Rows are appended and removed by walking `get_first_child` / `get_next_sibling`.

`_expanded` starts `False`. `_apply_expanded_state`:

- Expanded: toggle label `"▼ Agents"`, scrolled visible.
- Collapsed: toggle label `"▶ Agents"`, scrolled hidden.

The summary label is visible in both states.

```python
def set_on_compact(self, cb) -> None:
    """cb(session_key: str) -> None. None clears."""

def set_on_clear(self, cb) -> None:
    """cb(session_key: str) -> None. None clears."""

def set_rows(self, rows: list[dict]) -> None:
    """Replace the body and the header summary.

    Each dict:
      session_key: str
      name: str
      usage_percent: float | None
      busy: bool
    Unknown keys are ignored. A non-dict entry is skipped.
    """
```

`set_rows` sets the summary label to `collapsed_summary(rows)`, destroys the previous row widgets, and appends one row per dict.

One row, horizontal, CSS `agent-dashboard-row`:

- Name label, the `name` string. xalign 0. Width request about 120.
- Percent label. `None` / NaN / negative → `"—"`. Otherwise `f"{pct:.0f}%"`.
- `Gtk.ProgressBar`, width request 80, height 6, `set_show_text(False)`. Fraction `0.0` when the percent is missing or negative, else `min(pct / 100.0, 1.0)`. CSS class `context-meter-low` below 70, `context-meter-medium` from 70 up to but not including 90, `context-meter-high` at 90 and above. No zone class when the percent is missing (an empty trough, not a green bar at zero).
- State label: `"working"` when `busy` is true, else `"idle"`.
- Compact `Gtk.Button(label="Compact")`. Click calls the compact callback with `session_key` when the callback is not `None`.
- Clear `Gtk.Button(label="Clear")`. `set_sensitive(not busy)`. Click calls the clear callback with `session_key` when sensitive and the callback is not `None`.

The view does not confirm Clear. The window does.

`toggle()` flips `_expanded` and calls `_apply_expanded_state`. Same contract as `ActivityDrawer.toggle`.

Line count: about +180.

### 2.3 `ui/window.py`

Stop mounting the event drawer.

- Delete the `ActivityDrawer()` construction, the `ActivityWiringHandler` construction, and the `wire()` call (the block at the current lines 849–864).
- Delete `from ui.views.activity_drawer import ActivityDrawer`.
- Construct `AgentDashboard` in that same early spot and store `self._agent_dashboard`.
- The paned block at the end of `_build` uses `self._agent_dashboard` as `set_end_child`. Position, shrink, and resize flags stay as they are.
- In the project-opened lambda, replace `self._activity_drawer.clear_events()` with `self._refresh_agent_dashboard()`.

Compose the membership callback. Do not drop the feed-bar update:

```python
self._project_handler.set_on_members_changed(
    lambda n, m: (
        self._on_feed_bar_update(n, len(m)),
        self._refresh_agent_dashboard(),
    )
)
```

`set_on_agent_start` and `set_on_agent_end` stay wired to the activity pill. Add the dashboard refresh beside those calls, do not replace them:

```python
self._agent_runtime_handler.set_on_agent_start(
    lambda sk: (
        self._activity_handler.on_agent_start(sk),
        self._refresh_agent_dashboard(),
    )
)
self._agent_runtime_handler.set_on_agent_end(
    lambda sk: (
        self._activity_handler.on_agent_end(sk),
        self._refresh_agent_dashboard(),
    )
)
```

The button-bar meter callback `_on_context_meter` stays. After it runs (including when it returns because the tab is not the reporting agent), call `self._refresh_agent_dashboard()`. Every breakdown updates the matching dashboard row. The button-bar rule is unchanged.

```python
def _refresh_agent_dashboard(self) -> None:
    dash = getattr(self, "_agent_dashboard", None)
    if dash is None or self._agent_runtime_handler is None:
        return
    from ui.agent_dashboard_model import local_member_keys
    from models.colors import color_for_special_agent
    handler = self._agent_runtime_handler
    registered = handler.get_special_agents()
    project = None
    if self._project_handler is not None:
        project = self._project_handler.get_active_project_name()
    if project:
        keys = local_member_keys(
            self._project_handler.get_project_members(project),
            registered,
        )
    else:
        keys = list(registered)
    rows = []
    for sk in keys:
        name = registered.get(sk) or sk
        agent_def = handler.get_special_agent_def(sk)
        role = getattr(agent_def, "role", "") if agent_def is not None else ""
        rt = handler.runtime_for(sk)
        busy = rt is not None and rt.is_loop_active(sk)
        bd = handler.get_last_breakdown(sk)
        pct = None if not isinstance(bd, dict) else bd.get("usage_percent")
        rows.append({
            "session_key": sk,
            "name": name,
            "usage_percent": pct,
            "busy": busy,
            "color": color_for_special_agent(role if isinstance(role, str) else ""),
        })
    dash.set_rows(rows)
```

`color` is accepted on the row and may be ignored by the first paint. Do not fail if the view does not read it. The name is the required identity.

Wire the view once after it exists:

```python
self._agent_dashboard.set_on_compact(self._on_dashboard_compact)
self._agent_dashboard.set_on_clear(self._on_dashboard_clear)
```

```python
def _on_dashboard_compact(self, session_key: str) -> None:
    handler = self._agent_runtime_handler
    if handler is None:
        return
    rt = handler.runtime_for(session_key)
    busy = rt is not None and rt.is_loop_active(session_key)
    handler.compact_conversation(
        session_key, "", protect_open_turn=busy,
    )
    handler.forget_breakdown(session_key)
    self._refresh_agent_dashboard()

def _on_dashboard_clear(self, session_key: str) -> None:
    handler = self._agent_runtime_handler
    if handler is None:
        return
    rt = handler.runtime_for(session_key)
    if rt is not None and rt.is_loop_active(session_key):
        self._refresh_agent_dashboard()
        return
    name = handler.get_special_agents().get(session_key, session_key)
    self._confirm_clear_agent(session_key, name)
```

`_confirm_clear_agent` copies the stop-all dialog in this file (`_confirm_stop_all` around the current line 1703). Differences:

- `text=f"Clear {name}'s context?"`
- `secondary_text="The conversation is wiped. This cannot be undone."`
- Affirmative button label `"Clear"`, `Gtk.ResponseType.OK`.
- Cancel is the default.
- On OK: `ok = handler.clear_conversation(session_key)`. When `ok` is true, `handler.forget_breakdown(session_key)`. Then `_refresh_agent_dashboard()`.
- Store the dialog on `self._clear_agent_dialog` the same way stop-all stores `self._stop_all_dialog`, so it is not collected while open.
- `_dispatched` guard, then `close()`.

`clear_conversation` returns `False` when the loop became active between the insensitive check and the confirm. Do not call `forget_breakdown` in that case. Refresh anyway so the row shows working and Clear goes insensitive.

`compact_conversation` returns a dict and does not raise for the refusal cases in its body. Do not inspect the dict to decide whether to forget the breakdown. Always forget after the call. A no-op compact still leaves the row on an em dash until the next report, which is the honest state when we did not recompute the wire.

Both click handlers run on the GTK thread. `compact_conversation` is the same synchronous call `/compact` already makes. Do not add a worker thread.

Line count: about +120, minus the drawer construction.

### 2.4 `ui/handlers/agent_runtime_handler.py`

```python
def forget_breakdown(self, session_key: str) -> None:
    """Drop the cached meter report for one session.

    The next get_last_breakdown returns None until a model call stores
    a new one. Used after dashboard compact and clear so the row does
    not keep the pre-action percent.
    """
    self._last_breakdown.pop(session_key, None)
```

`_last_breakdown` is a plain dict on the handler. No lock. Callers are the GTK thread.

Line count: about +12.

### 2.5 `ui/styles.py`

Add classes next to the existing `.context-meter-*` block. Reuse those meter classes on the dashboard bar. Do not redefine them.

```css
.agent-dashboard-header {
    padding: 4px 8px;
}
.agent-dashboard-row {
    padding: 2px 8px;
}
```

Line count: about +8.

### 2.6 `tests/test_agent_dashboard_model.py` (new)

No GTK.

- `local_member_keys(["agent:coder", "gateway:x", "agent:supervisor"], {"agent:coder": "Coder", "agent:supervisor": "Supervisor"})` returns `["agent:coder", "agent:supervisor"]` in that order.
- A registered key that is not a member is excluded.
- `collapsed_summary([])` is `"No agents"`.
- A row with `usage_percent=12.4` renders `"Coder 12%"`.
- `usage_percent=None` renders `"Coder —"`.
- Two rows join with `" · "`.
- `True` as a percent renders an em dash, not `"1%"`.

Line count: about +60.

### Files NOT changed

- `ui/views/activity_drawer.py` and `tests/test_activity_drawer.py` — the class stays, it is no longer mounted.
- `ui/handlers/activity_wiring_handler.py` — not constructed, so `wire()` does not run and will not call `append_event` on a missing drawer.
- `ui/views/main_content.py` `set_context_meter` — the button-bar meter. The project tab still has no agent key. The dashboard is the surface for every agent.
- `compact_conversation` and `clear_conversation` — called, not edited.
- Feed handler, review bar, project-bar state machine.

---

## 3. Data Flow

### Open the drawer

1. The paned’s end child is `AgentDashboard`, collapsed. Header reads `"▶ Agents"` and `collapsed_summary` of the current rows.
2. Click calls `toggle()`. Header becomes `"▼ Agents"`. The scrolled body shows.
3. `_refresh_agent_dashboard` built the rows from `get_active_project_name`, then `get_project_members`, filtered by `local_member_keys` against `get_special_agents`. No project: `list(registered)`.

### A coder finishes a step

1. Runtime dispatches `_on_token_breakdown` on the GTK thread.
2. The handler stores `_last_breakdown[session_key]`.
3. `_on_context_meter` updates the button-bar meter only when that key is the visible tab.
4. `_refresh_agent_dashboard` runs regardless. That coder’s row percent and bar update. Other rows stay.

### Turn starts or ends

`set_on_agent_start` / `set_on_agent_end` still update the pill, then `_refresh_agent_dashboard`. `runtime_for(sk).is_loop_active(sk)` flips the state label and the Clear button’s sensitivity.

### Compact

1. Row button calls `_on_dashboard_compact(session_key)`.
2. `busy = runtime_for(sk) is not None and is_loop_active(sk)`.
3. `compact_conversation(sk, "", protect_open_turn=busy)`.
4. `forget_breakdown(sk)`.
5. Refresh. The row percent is an em dash. Clear follows the new busy flag.

### Clear

1. If the loop is active, refresh and return. `clear_conversation` is not called.
2. Otherwise the warning dialog. Cancel closes it and does not call clear.
3. OK calls `clear_conversation(session_key)`. `True` → `forget_breakdown`. `False` → leave the cached breakdown (the wipe did not happen).
4. Refresh.

### Membership

`toggle_agent` calls the members-changed callback with `(project_name, members)`. The lambda updates the feed bar count and refreshes the dashboard. A removed agent disappears. An added local agent appears with an em dash until their first model call.

---

## 4. File Change Summary

| File | Change | Lines (about) | Risk |
|---|---|---|---|
| `ui/agent_dashboard_model.py` | New pure helpers | +40 | Low |
| `ui/views/agent_dashboard.py` | New view | +180 | Medium |
| `ui/window.py` | Mount dashboard, unmount event drawer, actions | +120 | High |
| `ui/handlers/agent_runtime_handler.py` | `forget_breakdown` | +12 | Low |
| `ui/styles.py` | Header and row padding | +8 | Low |
| `tests/test_agent_dashboard_model.py` | New | +60 | Low |
| `docs/ARCHITECTURE.md` | §8 | +20 | Low |

---

## 5. Implementation Order

1. Model helpers and `tests/test_agent_dashboard_model.py`. Run that file.
2. `AgentDashboard` view. No window wiring yet.
3. `forget_breakdown`.
4. Window: construct the dashboard, paned end child, refresh, compact, confirm-clear. Remove drawer construction and `wire()`.
5. Compose project-open, members-changed, agent start/end, and the breakdown listener so each still does its old work and also refreshes.
6. CSS.
7. `docs/ARCHITECTURE.md`.
8. Run §6.

Do not leave `ActivityWiringHandler.wire()` pointed at a dashboard. `wire()` calls `append_event`.

---

## 6. Acceptance Criteria

- [ ] The paned under the chat is an `AgentDashboard`. `ActivityDrawer` is not constructed in `window.py`. `ActivityWiringHandler.wire()` is not called.
- [ ] Collapsed header shows `"▶ Agents"` and a summary. Expanded shows `"▼ Agents"` and one row per local project member.
- [ ] A project with Coder and Supervisor shows those two rows, in member-list order. A non-`agent:` member is not a row.
- [ ] No open project shows every `get_special_agents()` entry.
- [ ] No breakdown: percent text is `"—"`, bar fraction is 0, no green/yellow/red class.
- [ ] Breakdown `usage_percent` 80: text `"80%"`, fraction 0.8, class `context-meter-medium`.
- [ ] `is_loop_active` true: state `"working"`, Clear insensitive. Compact still clicks and passes `protect_open_turn=True`.
- [ ] Idle Clear opens the warning dialog. Cancel does not call `clear_conversation`. OK calls it. `True` forgets the breakdown.
- [ ] `clear_conversation` returning `False` does not forget the breakdown.
- [ ] Compact always forgets that session’s breakdown, then the row shows `"—"`.
- [ ] A breakdown for agent A updates A’s row while the visible tab is `project:…`. The button-bar meter still does not paint that report on the project tab.
- [ ] Membership change updates the feed-bar count and the dashboard rows.
- [ ] `tests/test_activity_drawer.py` still passes. The class remains, unwired.
- [ ] `tests/test_agent_dashboard_model.py` passes.

```bash
python3 -m pytest \
  tests/test_agent_dashboard_model.py \
  tests/test_activity_drawer.py \
  -q
```

Use the project venv if `python3 -m pytest` has no pytest (` .venv/bin/pytest`).

---

## 7. Edge Cases

| Case | Expected behavior |
|---|---|
| Project chat is the visible tab | Dashboard rows still update. The button-bar meter does not. |
| Agent has never had a model call | Percent `"—"`. Not `0%`. |
| `usage_percent` is `0` | `"0%"` and a zero-width green bar (`context-meter-low`). A real report. |
| `usage_percent` is `True` | Treated as missing. Em dash. |
| Loop active, Clear clicked | Button is insensitive, so the click does not fire. The confirm method also returns when `is_loop_active` is true. |
| Loop starts after the confirm dialog opened | `clear_conversation` returns `False`. Breakdown is kept. Refresh shows working. |
| Compact while working | `protect_open_turn=True`. Open turn is kept. Cached percent is dropped. |
| Compact while idle | `protect_open_turn=False`. |
| `runtime_for` returns `None` | Not busy. Compact and Clear still call the handler. Clear is allowed. `compact_conversation` returns the zero dict when the agent is missing. |
| No project open | Rows are `list(get_special_agents())`. |
| Empty member list | Summary `"No agents"`. No rows. |
| `get_project_members` contains a key not in `get_special_agents` | Skipped. |
| Two dashboards | There is one, the paned end child. |
| Event drawer tests | Still import `ActivityDrawer`. They do not need the window. |
| Feed bar member count | `set_on_members_changed` still calls `_on_feed_bar_update`. |

---

## 8. ARCHITECTURE.md Updates Required

- Directory tree: add `ui/views/agent_dashboard.py` and `ui/agent_dashboard_model.py`. Note `activity_drawer.py` is no longer mounted from `window.py`.
- Replace the “activity drawer is the paned end child” sentence with: the paned end child is `AgentDashboard`. It lists local project members and calls back to the window for compact and clear.
- `ActivityWiringHandler` is not started from `_build`.
- `forget_breakdown(session_key)` on `AgentRuntimeHandler`.

---

## 9. Implementer self-check

1. `window.py` does not reference `_activity_drawer` or `ActivityWiringHandler`.
2. `set_on_members_changed` still calls `_on_feed_bar_update`.
3. Clear’s dialog uses `Gtk.ResponseType.CANCEL` as the default. OK is the only path into `clear_conversation`.
4. Paste the pytest output from §6.
5. `grep -n "append_event" ui/window.py` returns nothing.
