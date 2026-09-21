# Investigation Request: Generation Counter May Drop ALL Deltas

## The situation

The generation-counter race fix (implemented today) was supposed to drop only
STALE deltas — those queued before completion but executed after. But the user
still sees agent messages truncate to the first word ("Type").

## The suspected bug in our fix

The generation counter increments in `_on_response_complete` (line 1421):

```python
def _on_response_complete(self, session_key, text):
    self._delta_generation[session_key] = self._delta_generation.get(session_key, 0) + 1
    if self._GLib is not None:
        self._GLib.idle_add(self._do_response_complete, session_key, text)
```

And deltas capture generation in `_on_text_delta` (line 978):

```python
def _on_text_delta(self, session_key, text):
    gen = self._delta_generation.get(session_key, 0)
    if self._GLib is not None:
        self._GLib.idle_add(self._do_text_delta, session_key, text, gen)
```

The flow from the BACKGROUND THREAD (agent/runtime.py `_run_loop`):
1. SSE chunk arrives → `self._dispatch(self._on_text_delta, sk, "Type")` → runtime's `_dispatch` does `GLib.idle_add(on_text_delta)` → **idle_add #1**
2. SSE chunk arrives → `self._dispatch(self._on_text_delta, sk, " " "test")` → **idle_add #2**
3. Stream ends → `self._dispatch(self._on_response_complete, sk, full_text)` → **idle_add #3**

On the MAIN THREAD idle queue:
- **idle #1 runs:** `_on_text_delta("Type")` → captures gen=0 → `idle_add(_do_text_delta, "Type", 0)` → **idle_add #4**
- **idle #2 runs:** `_on_text_delta(" test")` → captures gen=0 → `idle_add(_do_text_delta, " test", 0)` → **idle_add #5**
- **idle #3 runs:** `_on_response_complete(full_text)` → increments gen to 1 → `idle_add(_do_response_complete)` → **idle_add #6**
- **idle #4 runs:** `_do_text_delta("Type", gen=0)` → 0 < 1 → **DROPPED!** ← THIS IS THE BUG
- **idle #5 runs:** `_do_text_delta(" test", gen=0)` → 0 < 1 → **DROPPED!**
- **idle #6 runs:** `_do_response_complete` → ends streaming, renders final bubble

Wait — but if ALL deltas are dropped, then `is_streaming()` never becomes True,
and `_do_response_complete` takes the non-streaming fallback path. So the final
bubble SHOULD still render via `render_sync`. Unless the non-streaming path
also has a bug...

Actually re-read: `_on_response_complete` increments gen BEFORE queueing
`_do_response_complete`. But the deltas captured gen=0 BEFORE the increment
(in idle #1/#2). So when the delta callbacks (idle #4/#5) run AFTER the
increment (which happened in idle #3), they see gen=0 < current_gen=1 and are
dropped.

BUT: `_do_response_complete` runs in idle #6 (AFTER idle #4/#5). At that
point, `is_streaming()` is False (no delta ever started streaming), so
`was_streaming=False`. The non-streaming fallback should render the text.

So the question is: WHY does the user see "Type"? If all deltas are dropped
and the non-streaming path renders the full text, the bubble should be
complete. Unless:
- The non-streaming path is also broken
- Or `_do_response_complete` somehow doesn't run
- Or there's yet another race

## What to investigate

1. Read `_do_response_complete` in full
   (`ui/handlers/agent_runtime_handler.py:1426+`). When `was_streaming=False`,
   what path does it take? Does it render the text?

2. Check: is `_do_response_complete` even running? The `_dispatch._wrap`
   try/except catches exceptions. If `_do_response_complete` crashes, the
   streaming widget stays.

3. Check: is there ANOTHER `_do_text_delta` arriving AFTER
   `_do_response_complete`? If the SSE stream is slow and a late delta
   arrives after idle #6, it would see gen=0 < current_gen=1 and be dropped.
   But what if a delta arrives with gen=0 and the generation was never
   incremented (first turn, gen stays 0)?

4. The REAL question: on the FIRST turn, gen starts at 0 for everything.
   `_on_response_complete` increments to 1. Deltas captured 0. 0 < 1 →
   dropped. But `is_streaming()` was never True. So `_do_response_complete`
   should take the non-streaming path and render. DOES IT?

5. Check if there's a race between `_do_text_delta` (which may have run as
   idle #4, been dropped) and the streaming-start logic. If
   `_do_text_delta` runs but is dropped, and then `_do_response_complete`
   runs, `was_streaming` is False. But what if `_do_text_delta` for a
   PREVIOUS turn (from a tool-call response) started streaming, and
   `_do_response_complete` for THIS turn sees `is_streaming=True` from
   that old bubble?

## Files to read

- `ui/handlers/agent_runtime_handler.py` — `_do_response_complete` (1426+),
  `_do_text_delta` (984+), `_on_text_delta` (970+), `_on_response_complete` (1415+)
- `ui/handlers/chat_render_handler.py` — `end_streaming` (555+),
  `render_sync` (326+), `_finalize` (571+)
- `agent/runtime.py` — `_dispatch` (415+), the streaming loop (1639+),
  `_run_loop` completion path (1245+)

## Output

Write concise findings to `docs/specs/DELTA-GEN-DEBUG-FINDINGS.md`.
Focus on the EXACT mechanism that causes "Type" to be the only visible text.
Use `prompts/adversarialDebugger.md`.
