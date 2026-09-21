# Investigation Request: Supervisor Agent Messages Truncate to First Word

## The Bug
When the Supervisor (special:supervisor) agent sends a message in the project
chat, the rendered bubble shows only the first word (e.g. "Send" or "Okay" or
"C"). The full text IS stored correctly in the conversation JSON (verified:
250+ chars). User messages render fine. Only agent (Supervisor) messages
truncate.

## Key observations
1. No `Gtk-WARNING` in terminal — Pango is NOT rejecting the markup
2. The full text is stored in `~/.config/crabcakes/conversations/special:supervisor.json`
3. `process_segments` + `format_markdown` + `Pango.parse_markup` all succeed on the text (verified in sandbox)
4. The truncation appears at the FIRST WORD boundary — suggesting the streaming widget is never replaced by the final bubble
5. User-typed messages render correctly (they go through `render_sync`, not streaming)
6. Agent messages go through the streaming path: `start_streaming` → `update_streaming` → `end_streaming` → `_finalize`

## The rendering pipeline for agent messages

```
AgentRuntimeHandler._on_response_complete (background thread)
  → GLib.idle_add(_do_response_complete)  [main thread]
    → _do_response_complete:
      → extract_crabcards (overwrites sb.plain_text with cleaned version)
      → self._crh.end_streaming(session_key, agent_name=resolved_name, render=bool(streaming_text.strip()))
        → end_streaming:
          → pops sb from _streaming_bubbles
          → defines _finalize closure
          → self._dispatch(_finalize)  [GLib.idle_add on main thread]
            → _finalize:
              → full_text = sb.plain_text
              → if is_in_container(sb.bubble, sb.container): sb.container.remove(sb.bubble)
              → if render: final_bubble = build_role_bubble(...); sb.container.append(final_bubble)
```

## Hypothesis

The streaming widget shows the last `set_text` value (plain text with cursor).
If `_finalize` never runs, or runs but crashes (exception swallowed by the
_dispatch try/except), the streaming widget stays visible with whatever
partial text was last rendered via `update_streaming`'s throttled `set_text`.

The user sees "Send" — the first word — which could mean:
- The streaming throttle only rendered the first delta (which contained "Send")
- `_finalize` was dispatched via GLib.idle_add but never executed (or crashed)

## What to investigate

1. **Does `_finalize` actually run?** Read `end_streaming` in
   `ui/handlers/chat_render_handler.py` (around line 555-615). Trace the
   `_dispatch` call. Is there a race condition where the GLib.idle_add
   callback is dropped or never fires?

2. **Does `_finalize` crash silently?** The `_dispatch._wrap` has a
   try/except that catches `Exception` and calls `_logger.exception`. But
   if `_finalize` raises, the streaming widget stays. Read `_dispatch`
   (around line 760-780).

3. **Is `is_in_container` returning False?** If the streaming widget
   was already removed (or never added to the container), `is_in_container`
   returns False, `remove` is skipped, and `append(final_bubble)` still
   runs — but the old streaming widget might still be visible.

4. **Is there a double-call to end_streaming?** If `end_streaming` is
   called twice (e.g. from both `_do_response_complete` and another
   cleanup path), the second call returns early (sb already popped), and
   the first `_finalize` closure may reference stale state.

5. **Is `build_role_bubble` crashing?** If `build_role_bubble` raises
   during widget construction (e.g. a GTK error), the exception is caught
   by `_dispatch._wrap`, logged, and the streaming widget is never replaced.

6. **Is the streaming throttle eating the text?** `update_streaming`
   throttles at 150ms. If the last `set_text` call happened when only
   "Send" had arrived, and subsequent deltas were throttled, the streaming
   widget shows "Send" until `_finalize` replaces it. If `_finalize` never
   runs, "Send" stays forever.

## Files to read
- `ui/handlers/chat_render_handler.py` — `end_streaming` (555-615),
  `update_streaming` (439-480), `_dispatch` (760-780), `start_streaming` (373-410)
- `ui/handlers/agent_runtime_handler.py` — `_do_response_complete` (1406-1500),
  `_do_text_delta` (990-1025)
- `ui/views/chat_bubble.py` — `build_role_bubble` (240+), `process_segments` (131+)

## Output format

Per `prompts/adversarialDebugger.md`. Report the root cause in BUG #[N] format.
Be concise — focus on the exact mechanism, not the full 11-section probe (this
is a targeted investigation, not a full audit). Write findings to a file at
`docs/specs/SUPERVISOR-TRUNCATION-FINDINGS.md`.
