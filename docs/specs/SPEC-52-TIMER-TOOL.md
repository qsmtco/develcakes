# SPEC-52: Timer Tool — scheduled agent prompts

**Date:** 2026-10-22
**Author:** Supervisor (per PM verbal request)
**Status:** Draft — for implementation
**Implements:** the timer/cron substrate sketched in project chat 2026-10-22
**Depends on:** SPEC-45 (feed_card tool — callback pattern),
SPEC-49 (work-system removal — this adds no task tracking), SPEC-48 (store patterns)
**Target branch:** main

> Architecture compliance: the tool handler lives in `agent/group_tools.py`
> (agent layer — no GTK), scheduling and delivery live behind a callback
> installed from `ui/window.py` (composition root), delivery rides the
> existing `send_to_special_agent` path. Foundation (`utils/`) stays
> GTK-free. No new card types beyond the existing enum.

---

## DISCOVERY

- Read `agent/group_tools.py` (full file, 400+ lines): group tool handlers
  are plain functions returning `ToolResult`; module-level `_xxx_cb`
  globals plus `set_xxx_callback(cb)` setters; handlers validate every
  field then call the callback. Existing handlers: `agent_context`,
  `diary`, `project_memory`, `jev_ask`, `canvas`, `talk`, `feed_card`.
- Read `agent/tools.py` (lines 85-129, 2150-2400): `_TOOLS[...]`
  registrations live inside `_register_tools()` (module-level dict);
  each entry is `(ToolDefinition(name, description, parameters,
  requires_approval=False), lambda ...)`. Setters `set_xxx_callback`
  forward to `agent.group_tools` via local import.
- Read `ui/window.py` (lines 218-257, 1029-1057, 1316-1320, 1340-1379):
  `_wire_group_tool_callbacks()` (called from `_build`, line 797)
  installs callbacks via the `agent.tools` setters;
  `self._agent_runtime_handler` assigned at line 218, `self._project_handler`
  at 435, `self._feed_handler` at 493; `from gi.repository import ... GLib`
  at line 35; `_post_feed_card(session_key, fields) -> ToolResult` is the
  model for a window-side callback.
- Read `ui/agent_runtime/session.py` (lines 409-484):
  `send_to_special_agent(session_key, text, reply_target=None)` warns and
  returns when the key is not registered, renders an error when
  `self._active_project is None`, then runs a turn. It is safe to call
  mid-turn on another session; on the same session the runtime serializes
  via the handler's per-session machinery (`_get_runtime` reuses the
  existing loop).
- Read `ui/handlers/agent_runtime_handler.py` (lines 668-757):
  `get_special_agents() -> dict[str, str]` (key→display name),
  `get_special_agent_def(sk)`, `special_agent_turn_state(sk) -> Any|None`.
- Read `ui/handlers/project_handler.py` (lines 74-140, 363-365, 617-619):
  `get_active_project_name()`, `get_active_project_path()`,
  `set_on_project_opened(cb)` appends to `self._on_project_opened`,
  `open_project` invokes each `cb(name, path)`.
- Read `utils/tool_output_store.py` + `utils/project_memory.py`:
  the manifest pattern (`Reads/Writes/Network/GTK/Imports` header),
  `slugify(name, max_len=40)` exists at
  `utils/project_memory.py:32`.
- Read `tests/conftest.py` (lines 231-262): the `fake_glib` fixture
  records `timeout_add`/`timeout_add_seconds` into `.armed` and
  `source_remove` drops the id — tests fire timers by calling
  `cb()` directly.
- Grep `set_timer|timers.json|cancel_timer` across the repo: **0 matches**
  — no naming collisions.
- Architecture owner: scheduling state is per-project UI/session state;
  the agent layer only sees a fire-payload callback (same split as
  `feed_card`).

---

## 1. Overview

**Problem.** Agents and the PM cannot schedule future prompts. Every
delivery is synchronous: a human or an agent must be present to send.
This also blocks the deferred dream-consolidation program (parked in
October for exactly this reason).

**Solution.** Two new group tools, `set_timer` and `cancel_timer`, plus a
window-side scheduler. `set_timer` takes a wall-clock delay, a target
agent, and a prompt payload; when the delay elapses the payload is
delivered to the agent as a normal turn via `send_to_special_agent`.
`cancel_timer` cancels by id.

**Scope.**

| In | Out |
|---|---|
| `set_timer` / `cancel_timer` group tools | Cron-expression parser |
| `GLib.timeout_add` scheduling (live session) | Firing while the app is closed |
| Duration (`45m`) and time-of-day (`14:30`) delays | Timezones beyond system-local |
| `repeat: daily` (re-arm after fire) | weekly/monthly/interval repeats |
| `.crabcakes/timers.json` persistence + boot sweep | OS-level cron / systemd integration |
| "timer expired offline" catch-up feed card | UI pills/badges per agent (follow-up) |

**Honest limitation, stated to every caller:** a GLib timer only fires
while the app runs. `timers.json` exists so a restart does not silently
lose pending timers: on project open, stale one-shots produce an
`agent_action` card; pending repeat timers re-arm.

## 2. Changes by File

### 2.1 `utils/timer_store.py` (NEW — ~120 lines)

Manifest header (copy the SPEC-48 style):

```python
# utils/timer_store.py — scheduled-timer persistence (SPEC-52).
#   Reads/Writes: <project>/.crabcakes/timers.json
#   Network: none   GTK: none   Imports: stdlib only (json, os, uuid, datetime)
```

```python
REPEATS = ("", "daily")          # "" = one-shot
PROMPT_MAX = 2000                # chars
STALE_GRACE = 120                # seconds; one-shots past this are "expired"

def load_timers(project_path: str) -> list[dict]
    # Missing/unreadable/corrupt JSON → [] (OSError, json.JSONDecodeError).

def save_timers(project_path: str, timers: list[dict]) -> bool
    # Atomic write: tmp file + os.replace, os.makedirs(exist_ok=True).
    # False on OSError.

def add_timer(project_path: str, *, target: str, prompt: str,
              fires_at: str, repeat: str = "") -> dict | None
    # Validates: target starts with "agent:", PROMPT_MAX, fires_at parses
    # via datetime.fromisoformat, repeat in REPEATS.
    # Mints {"id": uuid4().hex[:8], "target": ..., "prompt": ...,
    #        "fires_at": ..., "repeat": ...}. Returns None on invalid args
    # or save failure.

def remove_timer(project_path: str, timer_id: str) -> bool
```

Traced: mirrors `tool_output_store.py`'s refuse-on-symlink discipline is
NOT needed (no path is derived from user input — only the fixed
`timers.json` name), but the atomic-write pattern is copied verbatim.

### 2.2 `agent/group_tools.py` (MODIFY — +~90 lines)

New callback global + setter, same shape as the others:

```python
_timer_cb = None

def set_timer_callback(cb) -> None:
    """Install the timer scheduling callback. Signature:
    (caller_session_key, target_session_key, timer: dict) -> ToolResult."""
    global _timer_cb
    _timer_cb = cb
```

Handler (module-level function; the caller's `session_key` and
`project_path` arrive via `**kwargs` exactly as in `feed_card`):

```python
def set_timer(delay: str, prompt: str, agent: str = "", repeat: str = "", **kwargs) -> ToolResult:
    session_key = kwargs.get("session_key", "")
    project_path = kwargs.get("project_path") or ""
```

Validation order (each failure returns `ToolResult(success=False, error=...)`):

1. `delay` non-empty str → parse. Accept `"90s"`, `"45m"`, `"2h"`,
   `"1h30m"`, or `"at HH:MM(:SS)"` (24h, system-local). Compute
   `fires_at` ISO string. A past time-of-day fires immediately (within
   1s) — do not error.
2. `prompt` str, non-blank, ≤ `PROMPT_MAX`.
3. `repeat` in `REPEATS` (`""` or `"daily"`).
4. Resolve target: `"self"` → caller's `session_key`; otherwise reuse
   `_resolve_peer(agent, session_key)` (group_tools.py:75) — an exact
   `agent:<role>` key, display name, or role. `daily` repeat requires an
   explicit non-self target is NOT required; self is allowed.
5. `_timer_cb` wired? Else `"set_timer is not wired"`.
6. Call `_timer_cb(session_key, target_key, timer_dict)` inside
   `try/except Exception` → `f"set_timer failed: {type(exc).__name__}"`,
   matching the `canvas` handler's shape (group_tools.py:338-342).

```python
def cancel_timer(timer_id: str, **kwargs) -> ToolResult:
    project_path = kwargs.get("project_path") or ""
```
→ `_timer_cb(session_key, None, {"timer_id": timer_id})`; the callback
interprets `target=None` as cancel. Empty id → error.

### 2.3 `agent/tools.py` (MODIFY — +~55 lines)

In `set_timer_callback(cb)` forwarding setter (after
`set_feed_card_callback`, line ~121, same 4-line shape), and inside
`_register_tools()` after the `feed_card` entry (line ~2363):

```python
    _TOOLS["set_timer"] = (
        ToolDefinition(
            name="set_timer",
            description=(
                "Schedule a prompt to be delivered to an agent after a "
                "wall-clock delay. delay is '90s'/'45m'/'2h'/'at 14:30'. "
                "agent defaults to self. repeat 'daily' re-arms. Returns "
                "the timer id. Timers only fire while the app is running."
            ),
            parameters={... delay, prompt, agent, repeat ...},
            requires_approval=False,
        ),
        lambda delay, prompt, agent="", repeat="", **kwargs: _group.set_timer(  # type: ignore
            delay, prompt, agent, repeat, **kwargs),
    )
    _TOOLS["cancel_timer"] = (...)
```

### 2.4 `ui/window.py` (MODIFY — +~120 lines)

In `_wire_group_tool_callbacks()` (after line 1318) — installs the
window-side scheduler:

1. `set_timer_callback(_schedule_timer)` where `_schedule_timer` holds
   `self`, a `dict timer_id -> GLib source id`, and a
   `threading.Lock`.
2. `_schedule_timer(caller_sk, target_sk, timer)`:
   - `target_sk is None` → cancel: `GLib.source_remove` if armed, drop
     from the store via `remove_timer`, return ok.
   - else: `add_timer(...)` persistence first (failure → tool error),
     then arm the GLib source. The handler may run on a tool-loop
     thread; keep the existing discipline and hop to the main loop with
     `GLib.idle_add(lambda: GLib.timeout_add(...))`, returning the
     recorded source id via a `threading.Event`, matching the `_canvas`
     pattern (window.py:1271-1303).
3. `_fire(self, timer)` runs ON the main loop (GLib source):
   - `daily` → advance `fires_at` by one day, re-persist, re-arm,
     return `GLib.SOURCE_CONTINUE`… NO — return False (a new source is
     armed by the re-arm call); one-shots return False and are removed
     from the store.
   - Deliver: `self._agent_runtime_handler.send_to_special_agent(
     timer["target"], f"[timer {timer['id']}]: {timer['prompt']}")`.
4. Boot sweep — in `_build`, after line 797's
   `_wire_group_tool_callbacks()`: read the active project path via
   `self._project_handler.get_active_project_path()`; for each loaded
   timer arm it if still future; a past **repeat** timer re-arms for the
   next day; a past one-shot older than `STALE_GRACE` posts an
   `agent_action` feed card (via the existing `FeedCardData` +
   `self._feed_handler.add_card(card)` construction copied from
   `_post_feed_card`, window.py:1358-1375) titled
   `"Timer {id} expired offline"`. NOT on boot — ProjectHandler's
   `set_on_project_opened(cb)` (project_handler.py:421) is the hook:
   the PM opens projects, not the window boot.

   NOTE: `project_handler` is constructed at line 435, AFTER line 797's
   wiring — so the `set_on_project_opened` registration must happen after
   `_project_handler` exists, i.e. in `_wire_projects()` (line 188) or
   late in `_build`. Implementer: place it after line 435's assignment.

### 2.5 `docs/ARCHITECTURE.md` — add `set_timer`/`cancel_timer` to the group-tool table (traced: the `feed_card` row at ~line 2492 is the template).

**Files NOT changed:**
- `ui/agent_runtime/session.py` — `send_to_special_agent` already handles
  unregistered keys (warns), no-project (renders error), and mid-turn
  re-entry. No changes.
- `utils/tool_output_store.py`, `utils/project_memory.py` — imported
  only (`slugify` is NOT needed; timer ids are `uuid4().hex[:8]`).
- `models/feed_card.py` — uses existing `agent_action` type; no enum change.

## 3. Data Flow

```
agent calls set_timer(delay="45m", prompt="re-audit the diff", agent="Debugger")
→ agent/tools.py lambda → group_tools.set_timer (validate all four fields)
→ _resolve_peer("Debugger", caller) → "agent:debugger"
→ _timer_cb("agent:coder", "agent:debugger", {"id":…, "fires_at":…})
→ window._schedule_timer → utils/timer_store.add_timer (persist)
→ GLib.idle_add hop → GLib.timeout_add(45*60*1000, _fire, …)
45m later, main loop:
→ _fire(timer) → send_to_special_agent("agent:debugger",
   "[timer ab12cd34]: re-audit the diff") → normal agent turn → reply
→ store cleaned (one-shot) / advanced (daily) → feed silent (the turn
  itself is the visible output)
```

Cancel: `cancel_timer("ab12cd34")` → same callback with `target=None` →
`GLib.source_remove` + `remove_timer`.

## 4. File Change Summary

| File | Change | Est. lines | Risk |
|---|---|---|---|
| `utils/timer_store.py` | NEW store | ~120 | Low |
| `agent/group_tools.py` | handlers + callback | ~90 | Low |
| `agent/tools.py` | registrations + setter | ~55 | Low |
| `ui/window.py` | scheduler + boot sweep | ~120 | Medium (threading) |
| `tests/test_timer_tool.py` | NEW tests | ~180 | — |
| `docs/ARCHITECTURE.md` | table rows | ~10 | — |

## 5. Implementation Order

1. `utils/timer_store.py` + unit tests (pure, no GTK) — verify with
   `pytest tests/test_timer_tool.py -k store`.
2. `agent/group_tools.py` handlers + registrations — tests with a fake
   callback.
3. `ui/window.py` scheduler with `fake_glib` (`timeout_add` recorded,
   fire `cb()` by hand — the conftest fixture's documented pattern).
4. Boot sweep + offline-expired card.
5. Full suite.

## 6. Acceptance Criteria

- [ ] `set_timer(delay="90s", prompt="ping")` from a test caller: with
      `fake_glib`, `fake_glib.armed[0]` is `(id, 90000, cb)`; calling
      `cb()` invokes the injected `send_to_special_agent` with
      `"[timer <id>]: ping"`.
- [ ] All six validation failures return distinct `ToolResult` errors.
- [ ] `cancel_timer` removes the source (`armed_ids` drops the id) and
      the store entry.
- [ ] `daily` timer fired by hand twice re-arms with
      `fires_at` advanced by one day each time.
- [ ] Boot sweep with a stale one-shot writes an `agent_action` card and
      a pending one-shot with `fires_at` in the future arms a new timer.
- [ ] `python -m pytest tests/` green; no GTK import reachable from
      `utils/timer_store.py` or `agent/group_tools.py` (grep `gi.repository`
      in both = 0).

## 7. Edge Cases

| Case | Behavior |
|---|---|
| `delay` in the past (`at 00:01` at 00:03) | fires immediately, no error |
| target agent not registered at fire time | `send_to_special_agent` warns, timer consumed |
| app closed across fire time | boot sweep: one-shot → expired card; daily → re-arm |
| `set_timer` with no open project | store path is the ACTIVE project; None → tool error `"no project open"` |
| mid-turn fire on the same agent | serialized by the existing per-session loop; documented, not blocked |
| corrupt `timers.json` | load → `[]`, file rewritten on next save |
| prompt > 2000 chars | rejected at the tool layer |

## 8. ARCHITECTURE.md Updates Required

- Group-tool table: `set_timer`, `cancel_timer` rows (callback contract,
  thread: caller's tool-loop thread for the call, main loop for fire).
- `.crabcakes/` file inventory: `timers.json`.
- Note the live-session limitation under a "Timers" heading.