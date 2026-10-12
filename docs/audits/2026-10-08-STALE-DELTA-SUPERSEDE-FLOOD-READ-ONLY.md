# Investigation Report — "dropping stale delta (token mismatch)" flood when telling an agent to stop mid-stream (READ-ONLY)

**Date:** 2026-10-08 ~23:55 PDT
**Scope:** develcakes working tree @ HEAD `600dfd42` (clean; the day's /clear + scroll fixes are committed). Investigation: static code reading + one isolated runtime probe (`/tmp/hermes_ghost_probe.py`) with `XDG_CONFIG_HOME` sandboxed, fake LLM/tool seams, no GTK, nothing persisted.
**Status:** DIAGNOSTIC ONLY — no code changed by this investigation; this report is the only artifact (untracked until committed).
**Severity:** MEDIUM aggregate —
1. LOW: debug-log flood (cosmetic; only visible with DEBUG on — main.py:21; the user runs `CRABCAKES_DEBUG=1`).
2. MEDIUM: a superseded ("ghost") turn is never stopped — it keeps streaming, keeps **executing tool batches**, and keeps making further LLM calls, unpiloted, until it naturally ends; its terminal is silently rejected. Tokens/compute are burned; tool side effects are real.
3. MEDIUM: "stop" is not immediate even when it IS a stop — cancellation is only honored at the next tool-loop iteration boundary; an in-flight LLM call always streams to completion (no check inside the chunk loop, no connection close).
**Related:** RACE-FIX v4 (turn tokens), SPEC-09 SP3 (stop-all), SPEC-02 (CANCEL_MESSAGE skipping the failure card), user report of 2026-10-08 (flood on `special:supervisor`).

---

## TL;DR — VERDICT

The flood is the RACE-FIX v4 **supersede-quarantine working exactly as designed**: sending any message to a session while its previous turn is mid-stream rotates `_turn_tokens[session_key]`; every remaining delta of the old turn now mismatches and is dropped — **one DEBUG line per raw delta**. The drops themselves are correct (they keep the old turn's text from polluting the new turn's UI) and are invisible in non-debug runs.

Behind the noise are two real costs, one of them probe-verified:

* **Ghost turn (probe-verified):** the superseded turn is not cancelled by the send. It finishes its in-flight LLM call, **runs its tool batch**, makes **further LLM calls**, and only then terminates — with its terminal **rejected** (`_terminate_turn: stale turn_token ... result rejected`, runtime.py:847-860). Only its UI callbacks are gated; execution is not.
* **Stop responsiveness:** even toolbar Stop-all does not touch an in-flight LLM stream — `_call_llm_streaming`'s chunk loop has no cancellation check (runtime.py:2563-2570; grep over :2494-2680 finds none) and nothing closes the connection. Cancellation only lands at the top of the next tool-loop iteration (runtime.py:1573-1604).

---

## 1. The exact line and its trigger

**The line:** `_on_text_delta: dropping stale delta (token mismatch) for %s` — producer-side guard at `ui/handlers/agent_runtime_handler.py:1904-1911` (log at :1907-1910).

**The guard:** the delta's captured turn token (threaded through `AgentRuntime._dispatch` — runtime.py:680-698, captured per-dispatch in a closure) does not match `_turn_tokens[session_key]`.

**`_turn_tokens[sk]` is rotated in ONE flow — a new turn start:**
* `send_to_special_agent` mints a fresh token: agent_runtime_handler.py:1327-1332, and *discards* the ended/completed flags for the session (:1325).
* `_run_loop` registers it for the new thread: runtime.py:1475.

**Therefore the flood REQUIRES a new send for the same session while the previous stream is still being consumed.** Typing any message mid-stream is exactly that send: the send path has **no active-loop guard and performs no cancel** (checked: no `active_loops`/busy check anywhere in `send_to_special_agent`; the only reference to active loops is the unrelated /clear refusal comment at :1477). The new turn starts while the old one keeps running.

**Why one line per delta (and not throttled):** the guard sits **before** the AC3 coalescing/throttle (:1912-1934) — every raw delta returns at the guard. The consumer-side twin guard (`_do_text_delta`, log at :2002-2005) only fires for coalesced dispatches that lose the race, so it is far quieter.

**Visibility:** `logger.debug` — enabled only by the DEBUG env (main.py:21: `_log_level = logging.DEBUG if get_env("DEBUG") else logging.WARNING`). The user's `CRABCAKES_DEBUG=1` is why they see it; production-quiet otherwise.

## 2. Why the old stream keeps flowing (no mid-stream cancellation exists)

* Cancellation checks live **only at the top of each tool-loop iteration**: runtime.py:1573 `while iteration < max_iter:` → :1575-1576 (`_cancel_requested`) and :1593-1594 (`session_key in self._cancelled`).
* `_call_llm_streaming` (def :2508) reads the SSE stream in `for ev in _stream:` (:2563) and dispatches every text delta (:2568). **No cancellation check anywhere in that loop** and nothing closes the HTTP/SSE connection (verified by grep across :2494-2680).
* Consequence: an in-flight LLM call **always streams to completion** — for both mid-stream sends AND toolbar stops. The loop can only react at the next iteration boundary. (Tool-execution subprocesses are separately killable — stop_all does kill registered process groups — but the LLM stream itself is not interruptible.)

## 3. Two stop variants → two different symptoms

| Trigger | Mechanism | What you see in DEBUG |
|---|---|---|
| **Typed message mid-stream** (the reported case — "tell the supervisor to stop / anything") | No cancel. `send_to_special_agent` rotates the token (:1327-1332) and **discards** the ended flags (:1325). Old stream keeps dispatching | `_on_text_delta: dropping stale delta (token mismatch)` — one line per remaining delta (the flood) |
| **Toolbar Stop-all** | `rt.stop_all()` (runtime.py:1268+) → `cancel()` (:1206-1265) which dispatches CANCEL_MESSAGE immediately with the ACTIVE token (:1258-1266) → handler `_do_error` marks the session ended (:3023-3025). Old stream still streams to completion | short burst of `dropping delta for ended session` (:1899-1903) — different guard, far fewer lines; then the turn stops at the next iteration boundary |

## 4. The ghost turn — PROBE-VERIFIED

Probe: `/tmp/hermes_ghost_probe.py` — headless `AgentRuntime`, fake `_call_llm` (per-token behavior, timed mid-flight window) and fake `_execute_tool_batch` (recorder, never executes), isolated `XDG_CONFIG_HOME`. Reproduces exactly the user's action: turn A mid-stream → send B ("stop") → observe.

Event timeline (seconds since probe start):

| t | Event |
|---|---|
| 6.579 | send A (`rt.send_message`, token A) |
| 6.903 | A's LLM call #1 entered; 3 pre deltas dispatched — **matched=True** (token A was current) |
| 6.903 | **SUPERSEDE: send B while A is mid-response** (token B) |
| 6.913 | B's loop registered token B; B's call returned; B completes normally |
| 7.603 | A's in-flight call **returns after the supersede**; 4 post deltas dispatched with token A — **matched=False for ALL FOUR** (this is the flood precondition) |
| 7.604 | **A's tool batch for iteration 1 RAN** (post-supersede, with `n_calls=1`) |
| 7.611 | **A entered iteration 2 — a second LLM call** |
| 7.611 | A terminates → `_terminate_turn` rejects it: `_terminate_turn: stale turn_token for probe:ghost (active=..., result=...); result rejected` (runtime.py:847-860) |

Final state: B `COMPLETED` (accepted); A's result absent from `_turn_results` — its `(sk, tokenA)` state stranded at `STREAMING`; `active_loops` empty.

**Conclusions (proven):** post-supersede the old turn (i) keeps streaming deltas (all dropped by the token guard — the flood), (ii) executes its tool batch, (iii) makes further LLM calls, (iv) is silently rejected at the end. UI callbacks are gated; **execution and conversation mutation are not** — no staleness check exists anywhere in the loop body (every `turn_token`/`_turn_tokens` reference in runtime.py enumerated: registration :1475, terminal reject :847-882, cancel :1262, state getters :2193-2209 — none in the 1500-2180 loop span).

**Approvals (reading-traced, not probed):** `_do_approval_needed` (agent_runtime_handler.py:1508+) has no token guard — in silent auto-accept mode it calls `approve_exec(..., True)` directly; otherwise it raises a pending card. A ghost turn can therefore still surface approval cards and run approved exec'd commands. Medium confidence — the full method was read; not exercised in the probe.

## 5. What is NOT broken (checked, clean)

* The drops are **correct by design** — superseded text cannot bleed into the new turn's streaming bubble or final render (that is RACE-FIX v4's purpose; the supersede contract is documented at :1449-1466).
* No delta corruption: accumulation (`_streaming_text`) happens only AFTER the guards (:1912+); dropped deltas mutate nothing.
* Cost accounting is consistent: ghost LLM calls still record usage against the session's conversation (`conv.record_usage`, runtime.py:~1719) — wasted, not misattributed.
* Orthogonal to the day's /clear and scroll fixes, and to the live tier (SPEC-19): no path there is involved; the guard predates it.

## 6. Fix directions (NOT applied — for PM decision)

**(a) Log hygiene — small, zero behavior risk.** First-drop-per-`(session_key, token)` logs; subsequent drops counted and (optionally) reported once on turn end. Kills the flood; keeps the diagnostic. One-file change in `_on_text_delta` (+twin).

**(b) Make stop/supersede actually stop — medium, touches RACE-FIX v4 territory.** Two pieces: a per-chunk freshness/cancel check inside `_call_llm_streaming`'s read loop (so the in-flight stream aborts/abandons promptly and the connection closes), and a per-iteration staleness bail in `_run_loop` (superseded token → terminate quietly at the next boundary). Needs kill-proof probes; risk is to the streaming retry path and the accepted-terminal accounting (the stale-reject must remain as the backstop).

**(c) Ghost gating — small-to-medium, safety net.** Token-check at the two execution gates: `_execute_tool_batch` entry and the approval path (`_do_approval_needed` / `_dispatch_approval` registration). Stale token → do not execute, do not raise a card; terminate quietly instead. This closes the dangerous part even if (b) is deferred.

**Note:** (b) and (c) implement one invariant ("a turn that is no longer the session's current turn performs no further work") at two layers — if both are wanted, spec them as one unit so the invariant is stated once and probed once per layer.

## Appendix — probe evidence

* File: `/tmp/hermes_ghost_probe.py` (throwaway; logic + timeline reproduced in §4).
* Run: `XDG_CONFIG_HOME=/tmp/dc_ghost_cfg .venv/bin/python3 /tmp/hermes_ghost_probe.py` (no GTK; headless).
* Raw key output: the §4 table is verbatim from the event log; the rejection line text is quoted from stderr (`_terminate_turn: stale turn_token for probe:ghost (active=<object…>, result=<object…>); result rejected`).
* Key cites: arh:1904-1911, 1899-1903, 1912-1934, 1325-1332, 1449-1466, 1508+, 3023-3025; runtime:680-698, 847-860, 1206-1265, 1268+, 1475, 1573-1604, 2508, 2563-2570, 2193-2209; main.py:21.
