# Audit Request: Race Fix v4c (producer-side token capture)

## What changed from v4b

v4b captured the token in `_on_*` (handler callback, main thread idle) — too
late, because `_turn_tokens` dict could have changed. Debugger found BUG #1
(CRITICAL): stale callback captures the NEW turn's token.

v4c captures the token at `_dispatch` time (runtime, background thread) —
stable for the entire turn. The token is passed as a `_turn_token` kwarg
through `_dispatch` → `_on_*` → `_do_*`.

## The token flow (v4c)

1. `send_to_special_agent` (main thread):
   - `new_token = object()`
   - `self._turn_tokens[sk] = new_token` (handler dict — the source of truth)
   - `rt._turn_token = new_token` (runtime field — captured by _dispatch)
   - `rt.send_message(sk, text)` → starts background thread

2. `_run_loop` (background thread):
   - Calls `self._dispatch(self._on_text_delta, sk, text)` etc.
   - `_dispatch` captures `token = self._turn_token` (runtime field, stable)
   - `_dispatch` calls `callback(*args, _turn_token=token)` via idle_add

3. `_on_*` (main thread idle):
   - Receives `_turn_token` kwarg (captured at dispatch time, not re-read)
   - Queues `_do_*(sk, text, token)`

4. `_do_*` (main thread idle):
   - Compares `token` against `self._turn_tokens.get(sk)` (handler dict)
   - If mismatch → stale → reject

## Why v4c fixes v4b's BUG #1

The token is captured at `_dispatch` time on the background thread. At that
point, `rt._turn_token` is stable (set by `send_to_special_agent` before
`send_message`). Even if `send_to_special_agent` is called again (turn B),
the background thread for turn A has already captured turn A's token in its
`_dispatch` closures. Turn B changes `rt._turn_token`, but turn A's queued
callbacks still carry turn A's token.

## The _dispatch change

```python
def _dispatch(self, callback, *args, **kwargs):
    if callback is None:
        return
    token = self._turn_token  # captured NOW (background thread, stable)
    def inner():
        try:
            callback(*args, **kwargs, _turn_token=token)
        except TypeError:
            callback(*args, **kwargs)  # callback doesn't accept _turn_token
        except Exception:
            logger.exception(...)
    ...
```

The `TypeError` fallback handles callbacks that don't accept `_turn_token`
(e.g., `on_tool_call_start`, `on_token_usage`).

## Key question

Is the `TypeError` fallback safe? If a callback has a `**kwargs` param, it
would accept `_turn_token` silently. If it doesn't, TypeError → fallback.
Could this mask a real TypeError inside the callback?

## File
- `agent/runtime.py` — `_dispatch` (line 419+), `_turn_token` field (line 373)
- `ui/handlers/agent_runtime_handler.py` — `_on_*` and `_do_*` methods

Write findings to `docs/specs/RACE-FIX-V4C-FINDINGS.md`.
