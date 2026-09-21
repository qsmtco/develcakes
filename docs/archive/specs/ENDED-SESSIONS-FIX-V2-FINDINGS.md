# `_ended_sessions` Race-Fix v2 Audit Findings

Scope: `ui/handlers/agent_runtime_handler.py`, per
`ENDED-SESSIONS-FIX-V2-AUDIT-REQUEST.md`; adversarial audit followed
`prompts/adversarialDebugger.md`.

## BUG #1 — HIGH: turn-A stale deltas still survive turn-B flag clearing

**Assumption violated:** Clearing the session flag in `send_to_special_agent`
means subsequent deltas belong to the new turn.

**Attack vector / reproduction:** Queue a turn-A `_do_text_delta`; complete A
(the flag is set); start turn B (`send_to_special_agent` discards the flag); then
run the queued A delta before B's callbacks. `_do_text_delta` has no turn
identity and accepts A's text, appending it to `_streaming_text` and potentially
starting B's bubble.

**Root cause:** `_ended_sessions` is only a session-level tombstone. It rejects
callbacks after completion until the next send, but cannot distinguish old
callbacks from new-turn callbacks after `discard()`.

**Fix:** Carry a per-turn generation/token through `_on_text_delta`,
`_on_response_complete`, and `_on_error`, or synchronously cancel/invalidate all
prior callbacks before starting a new turn.

## BUG #2 — MEDIUM: repeated completion can render duplicate final bubbles

**Assumption violated:** `_do_response_complete` is invoked exactly once per
turn.

**Attack vector / reproduction:** Invoke `_do_response_complete(sk, "answer")`
twice (duplicate provider completion, retry callback, or accidental duplicate
idle dispatch). First invocation sets the flag and finalizes streaming. On the
second invocation `was_streaming` is false and `text` is nonempty, so the
non-streaming fallback calls `render_sync` and appends another final bubble.
The flag is idempotent and does not prevent this.

**Root cause:** The new flag is only a stale-delta/tool-start guard; completion
has no completed/idempotency check.

**Fix:** Track a completion token/state and make duplicate completion callbacks
return without rendering, or ensure the runtime callback contract enforces
exactly-once delivery and test that contract.

## Question answers

1. **Ordering B replacement vs duplicate:** For one normal completion, it
replaces correctly. The stale delta makes `is_streaming(session_key)` true;
completion records `was_streaming=True`, then `end_streaming()` removes the
streaming bubble and schedules its `_finalize`, which appends one final bubble.
The `if not was_streaming and text` fallback is skipped, so it does not create a
second bubble. Note that `end_streaming()` itself dispatches finalization
asynchronously, but this does not trigger the fallback because `was_streaming`
is captured before the call.

2. **Tool-start interference:** A tool-start callback queued before completion
runs before the completion callback and is accepted. A tool-start callback that
runs after completion is suppressed, which is correct for a stale/late callback:
the runtime dispatches tool-start before the final response-complete dispatch.
No same-turn legitimate tool call should be generated after completion. The
existing comments/tests around `_started_turn_sessions` are stale/misleading:
the current `_do_tool_call_start` shown here does not clear the ended flag.

3. **Early return:** `_do_response_complete` still checks `if self._crh is None:
return` before setting the flag (lines 1443–1454). In that state, later stale
deltas are not guarded by this completion. `_do_error` sets the flag first and
has no analogous pre-flag return. If `_crh` is absent, no rendering is possible,
but the stated invariant “completion marks ended immediately” is not met.

4. **Previous BUG #2:** **Still applies.** `send_to_special_agent` discards the
flag before `rt.send_message`; any queued turn-A delta can pass the guard during
turn B. Moving the add to the top fixes the before/after-completion queue race,
not cross-turn callback identity.

## Scope/test audit

The requested four changes are present: add at the top of completion/error,
text-delta guard, and discard only in `send_to_special_agent`; no generation
counter remains. Existing tests cover basic ended-session suppression but do not
cover Ordering B with assertions against duplicate widgets, stale A callbacks
after B starts, duplicate completion, or `_crh is None` completion.
