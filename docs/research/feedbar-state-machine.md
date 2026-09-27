# FeedBar + ActivityHandler State Machine — Deep Investigation

> **Status: SUPERSEDED** — FeedBar (`ui/views/feedbar.py`) was deleted in SPEC-07 (R4,
> 2026-09-27). The 6-state machine survives in ActivityHandler but now renders plain
> text + state to the chat surface's activity pill via the status_target duck-type
> (`ActivityPillAdapter`) — no Pango markup, no progress bar. Accurate as of 2026-05-09
> for the deleted widget; the state-transition table below remains valid history.

**Date:** 2026-04-23
**Scope:** Read-only investigation of the feed bar and activity state machine, with focus on pre-flight behavior.

---

## The State Machine: 6 States

| State | Label | Color | Icon | What It Means |
|-------|-------|-------|------|---------------|
| `idle` | `● Idle` | Green `#4ade80` | Circle | Nothing happening. Progress bar pulsing subtly (idle pulse timer) |
| `sending` | `⬡ Pre Flight Check` | Amber `#f59e0b` | Hexagon | User pressed Send. Message is on its way to the gateway. **Pre-flight phase.** |
| `reasoning` | `◉ Reasoning…` | Amber `#f59e0b` | Filled circle | Gateway confirmed receipt (`res` received). Agent is thinking. |
| `streaming` | `⬇ Generating…` | Blue `#3b82f6` | Down arrow + live counters | First chat delta arrived. Shows token estimate, velocity (tok/s), elapsed time. |
| `tool_use` | `⚙ {tool_name}` | Purple `#a855f7` | Gear | Agent is executing a tool call. Tool name displayed. |
| `done` | `✓ Done` | Green `#4ade80` | Checkmark | Agent finished. Auto-transitions to `idle` after 5 seconds (done flash timer). |

---

## State Transitions — Full Map

```
idle ──[Send button]──→ sending (pre-flight)
sending ──[gateway res]──→ reasoning (phase 2 begins)
sending ──[30s timeout]──→ idle (pre-flight timeout, no response)
reasoning ──[first chat delta]──→ streaming
reasoning ──[tool_call event]──→ tool_use
reasoning ──[agent phase=end]──→ done
streaming ──[tool_call event]──→ tool_use
streaming ──[agent phase=end]──→ done
tool_use ──[first chat delta]──→ streaming
tool_use ──[another tool_call]──→ tool_use (refreshes tool name)
tool_use ──[agent phase=end]──→ done
done ──[5s timer]──→ idle (auto)
any ──[agent phase=error]──→ idle
```

---

## Pre-Flight — Complete Lifecycle

Pre-flight is the gap between **user pressing Send** and the **gateway confirming receipt**.

### 1. Trigger: Send Button Pressed

**`ChatHandler.on_send()`** (`chat_handler.py:267-268`):
```python
if self._on_send_initiated:
    self._on_send_initiated(session_key)
```

This fires **after** the message is dispatched to the gateway (via `GLib.idle_add`) but **before** any confirmation comes back. Wired in `window.py:856`:
```python
self._chat_handler.set_on_send_initiated(self._activity_handler.on_send_initiated)
```

### 2. ActivityHandler.on_send_initiated() — Enter Pre-Flight

**`activity_handler.py:101-114`**:

When called, it:
1. Stops any existing pre-flight timer for this session (`_stop_send_initiated_timer`)
2. Resets progress to **phase 1** (`_reset_progress`) — sets `_progress_start_time`, `_phase[sk] = 1`, `_event_hop_count = 0`
3. Transitions state to `"sending"` → FeedBar shows `⬡ Pre Flight Check` with amber color
4. Starts a **30-second GLib timer** (`PREFlight_TIMEOUT_SEC = 30`) — the pre-flight timeout

### 3. Progress Bar During Pre-Flight (Phase 1 — Time-Driven)

**`_compute_progress_fraction()`** (`activity_handler.py:307-322`):

While in phase 1, the progress bar is designed to crawl from **0% → 85%** over **60 seconds** based on elapsed time since send:
```python
if phase == 1:
    elapsed = time.monotonic() - start
    return min(elapsed / 60.0, 0.85)
```

So at 3 seconds you'd see ~5%, at 15 seconds ~25%, at 30 seconds ~50%. It never exceeds 85% during pre-flight.

**⚠️ Notable finding:** The `_live_update_timer` (200ms repeating) is only started for `reasoning`, `streaming`, and `tool_use` states. It is **not** started for `sending`. This means the progress bar's time-driven crawl computes **exactly once** when entering `sending`, and never updates again during the pre-flight wait. The bar stays static until `on_res_confirmed` transitions to `reasoning` (which starts the live timer).

### 4. Pre-Flight End: Gateway `res` Event

When the gateway confirms the message was received, `on_gateway_event()` routes it:

**`activity_handler.py:176-177`**:
```python
elif event == "res":
    self.on_res_confirmed(session_key)
```

**`on_res_confirmed()`** (`activity_handler.py:117-126`):
1. Stops the 30-second pre-flight timer
2. Switches to **phase 2** (`self._phase[sk] = 2`) — event-driven progress
3. Records agent start time
4. Transitions to `"reasoning"` → FeedBar shows `◉ Reasoning…`

### 5. Pre-Flight End: Timeout (No Response)

If 30 seconds pass with no `res` event:

**`_on_preflight_timeout()`** (`activity_handler.py:372-379`):
1. Removes the timer from the dict
2. If still in `"sending"` state and UI is showing this session:
   - Resets all session state (progress, phase, hop count, start time)
   - Transitions to `"idle"` → FeedBar shows `● Idle`
3. Returns `False` (don't re-run)

---

## Pre-Flight Timer Inventory

| Timer | Storage | Type | Interval | Purpose |
|-------|---------|------|----------|---------|
| `_send_initiated_timers` | `dict[str, int]` (session_key → GLib source ID) | `GLib.timeout_add_seconds(30)` | 30s one-shot | Pre-flight timeout — revert to idle if no `res` |
| `_live_update_timer` | Single `int or None` | `GLib.timeout_add(200)` | 200ms repeating | Live counter updates during reasoning/streaming/tool_use (NOT during sending) |
| `_done_flash_timers` | `dict[str, int]` (session_key → GLib source ID) | `GLib.timeout_add_seconds(5)` | 5s one-shot | Done → idle auto-transition |
| `_idle_pulse_timer` | Single `int or None` | `GLib.timeout_add(200)` | 200ms repeating | Pulse animation when idle |

---

## FeedBar — Widget Structure

```
FeedBar(Gtk.Box, HORIZONTAL, height=40)
  └── inner(Gtk.Box, VERTICAL, margins)
        ├── _status_label (Gtk.Label, left-aligned)
        └── _progress_bar (Gtk.ProgressBar, CSS class "response-progress")
```

### FeedBar Public API (called only by ActivityHandler)

| Method | What |
|--------|------|
| `set_status_text(markup)` | Updates label with Pango markup |
| `set_progress_fraction(0.0–1.0)` | Sets bar fill, makes visible |
| `set_progress_hidden(bool)` | Opacity 0 or 1 |
| `set_progress_opacity(0.0–1.0)` | Fine-grained opacity |
| `set_progress_pulse(bool)` | Start/stop pulse animation |
| `pulse_progress()` | Advance pulse one step |

---

## Two-Phase Progress Tracking

| Phase | Trigger | Progress Method | Range |
|-------|---------|-----------------|-------|
| **Phase 1** (time-driven) | `on_send_initiated()` | `elapsed / 60.0` | 0% → 85% over 60s |
| **Phase 2** (event-driven) | `on_res_confirmed()` | `0.05 + hops * 0.02` | 5% + 2% per gateway event, capped at 85% |

Phase 2 starts when the gateway confirms receipt. Each subsequent gateway event (agent, chat, tool_call, tick, etc.) increments `_event_hop_count`, advancing the progress bar by 2% per hop.

**Note:** Phase 2 progress updates are currently **disabled** (commented out 2026-04-22) due to a UI freeze investigation. See `on_gateway_event()` for details.

---

## Known Issues

1. **Progress bar doesn't animate during pre-flight.** The `_live_update_timer` is only started for `reasoning`/`streaming`/`tool_use`. During `sending`, the progress fraction computes once on entry and stays static. To animate pre-flight, `_set_state("sending")` would need to also start `_live_update_timer`.

2. **Phase 2 progress updates disabled.** Commented out in `on_gateway_event()` due to suspected UI freeze from too many `_update_feedbar()` calls during high-event bursts (e.g. large paste). Needs throttling.

---

*Source files: `ui/views/feedbar.py`, `ui/handlers/activity_handler.py`, `ui/handlers/chat_handler.py`, `ui/window.py`*
