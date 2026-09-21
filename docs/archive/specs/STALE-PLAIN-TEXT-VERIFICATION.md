# Verification Request: Truncation Root Cause = Stale sb.plain_text

## The symptom

Agent messages truncate to the first word ("Type", "Send", "Okay"). No
exceptions, no Pango warnings, no token mismatches in the terminal. The full
text IS stored in the conversation JSON.

## The terminal evidence

```
agent.runtime DEBUG [stream] done: text_len=546
agent.runtime DEBUG [tool-loop] dispatching on_response_complete len=546
ui.handlers.agent_runtime_handler DEBUG _do_response_complete: was_streaming=True text_len=546
```

No errors. The runtime produced 546 chars. `_do_response_complete` ran.
`was_streaming=True`. Yet the user sees only "Type".

## The claimed root cause

`_finalize` (in `chat_render_handler.py:end_streaming`) renders `sb.plain_text`:
```python
def _finalize():
    full_text = sb.plain_text  # ← THIS
    ...
    final_bubble = build_role_bubble(sb.role, full_text, ...)
```

`sb.plain_text` is updated by `update_streaming()`:
```python
def update_streaming(self, session_key, delta_text):
    sb = self._streaming_bubbles[session_key]
    sb.plain_text = delta_text  # updated BEFORE throttle check
    # ... throttle skips set_text, but plain_text IS set ...
```

BUT `update_streaming` is called from `_do_text_delta`, which has its OWN
throttle:
```python
def _do_text_delta(self, session_key, text, delta_token=None):
    ...
    self._streaming_text[session_key] += text  # ALWAYS accumulated
    ...
    # Handler throttle (50ms):
    now = time.monotonic()
    last = self._last_delta_dispatch.get(session_key, 0.0)
    if now - last >= self._delta_throttle_sec:
        self._last_delta_dispatch[session_key] = now
        self._crh.update_streaming(session_key, self._streaming_text[session_key])
    # ↑ ONLY called when NOT throttled
```

When the handler throttles (skips), `update_streaming` is NEVER called, so
`sb.plain_text` is NEVER updated with the later chunks. `_streaming_text[sk]`
(the handler dict) IS accumulated (line before the throttle), but that dict
is not what `_finalize` reads.

Result: `sb.plain_text` has only the text from the LAST non-throttled
`update_streaming` call. For fast-streaming responses, that's often just the
first chunk. `_finalize` renders that partial text. User sees "Type".

## The claimed fix

In `_do_response_complete`, BEFORE calling `end_streaming`, overwrite
`sb.plain_text` with the authoritative full `text` argument:

```python
streaming_text = self._crh.get_streaming_text(session_key) or ""
if was_streaming and text and len(text) > len(streaming_text):
    self._crh.set_streaming_text(session_key, text)
    streaming_text = text
self._crh.end_streaming(session_key, ...)
```

## What to verify

1. **Is the root cause correct?** Is `sb.plain_text` really stale because the
   handler throttle skips `update_streaming`? Or is there another explanation?

2. **Is the fix correct?** Does overwriting `sb.plain_text` with `text` before
   `end_streaming` guarantee `_finalize` renders the full text?

3. **Could `text` (the runtime argument) ever be SHORTER than the actual full
   response?** The runtime passes `text_content` from
   `extract_text_content(response)`. Could this miss text that was streamed
   via deltas?

4. **Is the `len(text) > len(streaming_text)` guard correct?** Could there be
   a case where `streaming_text` is correct but `text` is wrong (e.g. crabcard
   extraction already cleaned `streaming_text`)?

5. **Does this interact with the Phase C crabcard extraction?** Lines above
   the fix call `set_streaming_text(session_key, cleaned)` after crabcard
   extraction. Does the new fix overwrite that cleaned text with the raw `text`?

## Files to read

- `ui/handlers/agent_runtime_handler.py` — `_do_text_delta` (1000+),
  `_do_response_complete` (1466+)
- `ui/handlers/chat_render_handler.py` — `update_streaming` (455+),
  `end_streaming` (555+), `_finalize` (571+)
- `agent/runtime.py` — `_call_llm_streaming` (1645+), the streaming loop

Write findings to `docs/specs/STALE-PLAIN-TEXT-FINDINGS.md`.
