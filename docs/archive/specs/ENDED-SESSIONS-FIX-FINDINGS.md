# `_ended_sessions` Race-Fix Audit Findings

Scope: `ui/handlers/agent_runtime_handler.py`, changes described in
`ENDED-SESSIONS-FIX-AUDIT-REQUEST.md`. Audited against
`prompts/adversarialDebugger.md`.

## BUG #1 — CRITICAL: completion-at-end does not protect an already queued stale delta

**Assumption violated:** `_do_response_complete` will mark the session ended before
any stale delta callback can run.

**Attack vector:** Arrange the normal idle queue order produced by the runtime:
`_on_text_delta` dispatches `idle_add(_do_text_delta, ...)`, then the runtime
later dispatches `idle_add(_do_response_complete, ...)`. Queue:
`[_do_text_delta(stale), _do_response_complete]`.

**Reproduction:** With `_ended_sessions` initially empty and no active bubble,
queue those two callbacks in that order. `_do_text_delta` reaches line 1007
before line 1542 has executed, accumulates the text, and starts a new streaming
bubble. `_do_response_complete` then finalizes that bubble and only adds the
session to `_ended_sessions` at line 1542. The stale callback was not dropped.

**Root cause:** The guard is in the consumer (`_do_text_delta`), but the tombstone
is installed only at the end of the later consumer (`_do_response_complete`).
GLib idle ordering cannot retroactively suppress callbacks already run. The long
render/card/lifecycle work between lines 1446–1540 widens the interval before the
flag is set.

**Fix:** Mark the session ended synchronously in `_on_response_complete` (before
`idle_add`) or at the very start of `_do_response_complete`, before any rendering.
A generation/turn identity remains the robust solution for distinguishing old
callbacks from a subsequently started turn.

## BUG #2 — HIGH: a new turn can reopen the stale-callback window

**Assumption violated:** Clearing `_ended_sessions` in `send_to_special_agent`
identifies only callbacks belonging to the new turn.

**Attack vector:** Complete turn A, leave old delta callbacks queued, then start
turn B. `send_to_special_agent` line 856 clears the sole per-session flag before
`rt.send_message`. A queued delta from turn A now passes line 1007 and can create
or append to turn B's bubble.

**Reproduction:** Queue `A_delta` without running it; run/complete A (or otherwise
set the ended flag); call `send_to_special_agent(B)` (flag is discarded); then
run `A_delta`. There is no generation/token in `_on_text_delta` or
`_do_text_delta` to reject it.

**Root cause:** A boolean/set keyed only by `session_key` cannot distinguish
previous-turn callbacks from current-turn callbacks. The reverted generation
counter avoided this class only if incremented at the correct turn boundary and
captured/checked consistently; the current implementation has no equivalent
identity.

**Fix:** Associate callbacks with a per-turn token/generation, or synchronously
invalidate/drain old callbacks before allowing a new turn. Do not rely on a
session-wide ended bit for cross-turn ordering.

## Question answers

1. **Before or after?** Either is possible by queue order. If completion runs
first, its final line sets the flag before a later stale delta and the guard
works. If the stale delta is already queued first (the ordinary producer order),
it runs before the flag exists and is accepted.

2. **Same bug in another form?** **Yes.** The stated queue
`[_do_text_delta(stale), _do_response_complete]` is a direct reproducer of the
same race; the new guard does not fix it.

3. **Gap after `end_streaming`?** **Yes.** `end_streaming` is called at line
1486, while `_ended_sessions.add` is line 1542. Any stale delta dispatched/reentrant
between those operations sees no flag and can start a replacement bubble after
the old one was removed/finalized.

4. **Can a legitimate first delta be blocked?** Under the normal single-turn
path, `send_to_special_agent` clears the flag synchronously before starting the
background runtime, so its first delta is not blocked. However, concurrent
same-session sends are not serialized: a new send can clear the flag while old
callbacks remain, causing the HIGH bug above. Also, `_do_response_complete` and
`_do_error` install the flag only after substantial work; exceptions or an early
`_crh is None` return can leave no tombstone at all.

## Scope/test audit

- All three requested code changes are present: no `_delta_generation` references,
`_do_text_delta` has the two-argument signature, and the discard is at line 856.
- The existing tests cover the guard and basic tool-only behavior, but do not
cover the critical queue order, the post-`end_streaming` gap, or stale turn-A
deltas after turn-B clears the flag. A regression test must invoke callbacks in
those exact orders and assert no new streaming bubble/text is created.
