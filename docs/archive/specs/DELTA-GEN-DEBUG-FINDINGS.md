# Delta Generation Debug Findings

## Exact idle-queue ordering: all deltas are dropped

The generation guard does drop **all** text deltas on the first turn when the runtime uses its two-stage dispatch. The queue is FIFO (same default GLib idle priority), so newly-added callbacks go behind callbacks already queued.

For chunks `"Type"`, `" test…"`, then completion:

1. Runtime background thread calls `_dispatch(_on_text_delta, "Type")`: runtime queues wrapper **A1**.
2. Runtime queues wrapper **A2** for the next delta.
3. Runtime queues wrapper **A3** for `_on_response_complete(full_text)`.
4. Main thread runs **A1**. `_on_text_delta` reads generation `0`, then queues `_do_text_delta("Type", 0)` as **B1**. Existing A2 and A3 remain ahead of B1.
5. Main thread runs **A2**. It queues `_do_text_delta(" test…", 0)` as **B2**. Queue is now A3, B1, B2.
6. Main thread runs **A3**. `_on_response_complete` increments generation from `0` to `1`, then queues `_do_response_complete(full_text)` as **C1**. Queue is now B1, B2, C1.
7. B1 runs: `delta_gen=0 < current_gen=1`, so it is dropped.
8. B2 runs: same check, so it is dropped.
9. C1 runs. `was_streaming` is false because no accepted delta reached `_crh.start_streaming()`.

Thus the suspected guard does not selectively remove late deltas: in this normal burst, it removes every delta because every B callback captured generation 0 before A3 advanced it.

## Consequence: this does *not* explain a visible `Type` by itself

With all deltas dropped, `_do_response_complete` takes the non-streaming branch at `agent_runtime_handler.py:1507-1527` and renders its authoritative `text` argument (`full_text`). The full-text fallback is not conditional on `_streaming_text`; it should produce the complete bubble.

A `Type`-only bubble requires a different ordering/state: at least one `_do_text_delta` must be accepted before completion, thereby creating a streaming bubble, while later deltas are dropped. However, with the shown two-stage `GLib.idle_add` FIFO ordering, B1/B2 are behind A3, so that mixed ordering cannot arise from this callback chain alone. If logs show `was_streaming=True` and only `Type`, another producer/callback or a pre-existing streaming bubble is involved (or queue priorities differ from the default FIFO assumption).

## Code evidence

- `agent/runtime.py:419-425`: every runtime callback is wrapped in `GLib.idle_add`; this creates A callbacks.
- `ui/handlers/agent_runtime_handler.py:978-980`: each A delta callback queues a second idle callback and captures generation 0.
- `ui/handlers/agent_runtime_handler.py:1417-1421`: A3 advances generation before queueing C1.
- `ui/handlers/agent_runtime_handler.py:1007-1013`: B callbacks are rejected before accumulation/start-streaming.
- `ui/handlers/agent_runtime_handler.py:1015-1018`: therefore no streaming bubble is created in the all-dropped ordering.
- `ui/handlers/agent_runtime_handler.py:1451`: C1 observes `was_streaming=False`.
- `ui/handlers/agent_runtime_handler.py:1507-1527`: C1 renders the full completion `text`.
- `ui/handlers/chat_render_handler.py:461-462`: only an accepted delta can populate `sb.plain_text`; dropped deltas cannot leave `Type` there.
- `ui/handlers/chat_render_handler.py:570-605`: the streaming finalizer renders only `sb.plain_text`, relevant only if a streaming bubble was already active.

## Conclusion

The generation fix has a real bug: it drops all queued deltas in the ordinary two-stage FIFO burst. It does **not**, on the code shown, account for the reported `Type`-only final bubble; that symptom requires proving `was_streaming=True` (or locating another callback path/queue priority). Instrument the A/B/C callbacks with sequence numbers and log `was_streaming`, `text_len`, and `sb.plain_text` immediately before `end_streaming` to distinguish these cases.
