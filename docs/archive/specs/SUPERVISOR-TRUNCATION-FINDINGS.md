# Supervisor Truncation Findings

## BUG #1
Severity: HIGH

**Assumption violated:** response-complete and text-delta idle callbacks execute in logical stream order.

**Attack vector:** A runtime worker dispatches `_on_text_delta` and then `_on_response_complete`. Each handler independently calls `GLib.idle_add`: `_on_text_delta` queues `_do_text_delta`, while completion queues `_do_response_complete`. Because these are nested idle callbacks, completion can run before the queued text-delta callback(s).

**Reproduction:** With `GLib` enabled, deliver a delta followed immediately by completion. The queue contains `_on_text_delta` then `_on_response_complete`; the first callback only queues `_do_text_delta`, the second queues `_do_response_complete`. `_do_response_complete` observes `is_streaming=False`, renders the full `text` through the non-streaming path, and clears `_streaming_text`. The delayed `_do_text_delta` then starts a new streaming bubble and displays only that delta (often the first word/character). No Pango warning occurs; the full text remains persisted.

**Root cause:** Double-dispatch race between `AgentRuntime._dispatch()` and `AgentRuntimeHandler._on_*` methods. Completion is not serialized behind all already-issued text deltas. The stale `_do_text_delta` also clears `_ended_sessions` and creates a post-completion streaming widget, leaving its throttled partial text visible.

**Fix:** Serialize the UI callbacks: either have `_on_text_delta`/`_on_response_complete` invoke their `_do_*` methods directly when already running in the runtime's GLib dispatch, or use one per-session FIFO/coalescing dispatcher. Completion must drain/apply the accumulated final text and invalidate queued stale delta callbacks before rendering.

## Evidence

- `ui/handlers/agent_runtime_handler.py:969-976`: `_on_text_delta` wraps `_do_text_delta` in a second `GLib.idle_add`.
- `ui/handlers/agent_runtime_handler.py:1396-1403`: `_on_response_complete` independently wraps `_do_response_complete` in `GLib.idle_add`.
- `ui/handlers/agent_runtime_handler.py:979-1024`: delayed `_do_text_delta` starts streaming after completion if no bubble exists.
- `ui/handlers/agent_runtime_handler.py:1406-1470`: completion clears accumulated state, checks `is_streaming`, and can take the non-streaming path before delayed deltas execute.
- `ui/handlers/chat_render_handler.py:546-627`: `end_streaming` pops the bubble and schedules another idle callback, adding a third dispatch layer.
- `ui/handlers/chat_render_handler.py:754-773`: `_dispatch` schedules callbacks with `GLib.idle_add`; exceptions are logged, so this is not a Pango failure.
